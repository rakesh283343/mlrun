# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import typing
import json
import socket
import uuid
from ast import literal_eval
from base64 import b64decode
from copy import deepcopy
from os import environ, makedirs, path
from pathlib import Path
from tempfile import mktemp

import yaml
from kfp import Client
from nuclio import build_file
import importlib.util as imputil

from .utils import retry_until_successful
from .config import config as mlconf
from .datastore import store_manager
from .db import get_or_set_dburl, get_run_db
from .execution import MLClientCtx
from .funcdoc import find_handlers
from .k8s_utils import get_k8s_helper
from .model import RunObject, BaseMetadata, RunTemplate
from .runtimes import (
    HandlerRuntime,
    LocalRuntime,
    RemoteRuntime,
    RuntimeKinds,
    get_runtime_class,
)
from .runtimes.base import FunctionEntrypoint
from .runtimes.utils import add_code_metadata, global_context
from .utils import (
    get_in,
    logger,
    parse_function_uri,
    update_in,
    new_pipe_meta,
    extend_hub_uri,
)


class RunStatuses(object):
    succeeded = 'Succeeded'
    failed = 'Failed'
    skipped = 'Skipped'
    error = 'Error'
    running = 'Running'

    @staticmethod
    def all():
        return [
            RunStatuses.succeeded,
            RunStatuses.failed,
            RunStatuses.skipped,
            RunStatuses.error,
            RunStatuses.running,
        ]

    @staticmethod
    def stable_statuses():
        return [
            RunStatuses.succeeded,
            RunStatuses.failed,
            RunStatuses.skipped,
            RunStatuses.error,
        ]

    @staticmethod
    def transient_statuses():
        return [
            status
            for status in RunStatuses.all()
            if status not in RunStatuses.stable_statuses()
        ]


def run_local(
    task=None,
    command='',
    name: str = '',
    args: list = None,
    workdir=None,
    project: str = '',
    tag: str = '',
    secrets=None,
    handler=None,
    params: dict = None,
    inputs: dict = None,
    artifact_path: str = '',
):
    """Run a task on function/code (.py, .ipynb or .yaml) locally,

    e.g.:
           # define a task
           task = NewTask(params={'p1': 8}, out_path=out_path)
           # run
           run = run_local(spec, command='src/training.py', workdir='src')

           or specify base task parameters (handler, params, ..) in the call

           run = run_local(handler=my_function, params={'x': 5})

    :param task:     task template object or dict (see RunTemplate)
    :param command:  command/url/function
    :param name:     ad hook function name
    :param args:     command line arguments (override the ones in command)
    :param workdir:  working dir to exec in
    :param project:  function project (none for 'default')
    :param tag:      function version tag (none for 'latest')
    :param secrets:  secrets dict if the function source is remote (s3, v3io, ..)

    :param handler:  pointer or name of a function handler
    :param params:   input parameters (dict)
    :param inputs:   input objects (dict of key: path)
    :param artifact_path: default artifact output path

    :return: run object
    """

    if command and isinstance(command, str):
        sp = command.split()
        command = sp[0]
        if len(sp) > 1:
            args = args or []
            args = sp[1:] + args

    meta = BaseMetadata(name, project=project, tag=tag)
    command, runtime = _load_func_code(command, workdir, secrets=secrets, name=name)

    if runtime:
        handler = handler or get_in(runtime, 'spec.default_handler', '')
        meta = BaseMetadata.from_dict(runtime['metadata'])
        meta.name = name or meta.name
        meta.project = project or meta.project
        meta.tag = tag or meta.tag

    fn = new_function(meta.name, command=command, args=args)
    meta.name = fn.metadata.name
    fn.metadata = meta
    if workdir:
        fn.spec.workdir = str(workdir)
    return fn.run(
        task,
        name=name,
        handler=handler,
        params=params,
        inputs=inputs,
        artifact_path=artifact_path,
    )


def function_to_module(code='', workdir=None, secrets=None):
    """Load code, notebook or mlrun function as .py module
    this function can import a local/remote py file or notebook
    or load an mlrun function object as a module, you can use this
    from your code, notebook, or another function (for common libs)

    Note: the function may have package requirements which must be satisfied

    example:

        mod = mlrun.function_to_module('./examples/training.py')
        task = mlrun.NewTask(inputs={'infile.txt': '../examples/infile.txt'})
        context = mlrun.get_or_create_ctx('myfunc', spec=task)
        mod.my_job(context, p1=1, p2='x')
        print(context.to_yaml())

        fn = mlrun.import_function('hub://open_archive')
        mod = mlrun.function_to_module(fn)
        data = mlrun.run.get_dataitem("https://fpsignals-public.s3.amazonaws.com/catsndogs.tar.gz")
        context = mlrun.get_or_create_ctx('myfunc')
        mod.open_archive(context, archive_url=data)
        print(context.to_yaml())

    :param code:    path/url to function (.py or .ipynb or .yaml)
                    OR function object
    :param workdir: code workdir
    :param secrets: secrets needed to access the URL (e.g.s3, v3io, ..)

    :return python module
    """
    command, runtime = _load_func_code(code, workdir, secrets=secrets)
    if not command:
        raise ValueError('nothing to run, specify command or function')

    path = Path(command)
    mod_name = path.name
    if path.suffix:
        mod_name = mod_name[: -len(path.suffix)]
    spec = imputil.spec_from_file_location(mod_name, command)
    if spec is None:
        raise OSError(f'cannot import from {command!r}')
    mod = imputil.module_from_spec(spec)
    spec.loader.exec_module(mod)

    return mod


def _load_func_code(command='', workdir=None, secrets=None, name='name'):
    is_obj = hasattr(command, 'to_dict')
    suffix = '' if is_obj else Path(command).suffix
    runtime = None
    if is_obj or suffix == '.yaml':
        is_remote = False
        if is_obj:
            runtime = command.to_dict()
        else:
            is_remote = '://' in command
            data = get_object(command, secrets)
            runtime = yaml.load(data, Loader=yaml.FullLoader)

        command = get_in(runtime, 'spec.command', '')
        code = get_in(runtime, 'spec.build.functionSourceCode')

        if code:
            fpath = mktemp('.py')
            code = b64decode(code).decode('utf-8')
            command = fpath
            with open(fpath, 'w') as fp:
                fp.write(code)
        elif command and not is_remote:
            command = path.join(workdir or '', command)
            if not path.isfile(command):
                raise OSError('command file {} not found'.format(command))

        else:
            raise RuntimeError('cannot run, command={}'.format(command))

    elif command == '':
        pass

    elif suffix == '.ipynb':
        fpath = mktemp('.py')
        code_to_function(name, filename=command, kind='local', code_output=fpath)
        command = fpath

    elif suffix == '.py':
        if '://' in command:
            fpath = mktemp('.py')
            download_object(command, fpath, secrets)
            command = fpath

    else:
        raise ValueError('unsupported suffix: {}'.format(suffix))

    return command, runtime


def get_or_create_ctx(
    name: str, event=None, spec=None, with_env: bool = True, rundb: str = ''
):
    """ called from within the user program to obtain a run context

    the run context is an interface for receiving parameters, data and logging
    run results, the run context is read from the event, spec, or environment
    (in that order), user can also work without a context (local defaults mode)

    all results are automatically stored in the "rundb" or artifact store,
    the path to the rundb can be specified in the call or obtained from env.

    :param name:     run name (will be overridden by context)
    :param event:    function (nuclio Event object)
    :param spec:     dictionary holding run spec
    :param with_env: look for context in environment vars, default True
    :param rundb:    path/url to the metadata and artifact database

    :return: execution context

    Example:

    # load MLRUN runtime context (will be set by the runtime framework e.g. KubeFlow)
    context = get_or_create_ctx('train')

    # get parameters from the runtime context (or use defaults)
    p1 = context.get_param('p1', 1)
    p2 = context.get_param('p2', 'a-string')

    # access input metadata, values, files, and secrets (passwords)
    print(f'Run: {context.name} (uid={context.uid})')
    print(f'Params: p1={p1}, p2={p2}')
    print('accesskey = {}'.format(context.get_secret('ACCESS_KEY')))
    print('file: {}'.format(context.get_input('infile.txt').get()))

    # RUN some useful code e.g. ML training, data prep, etc.

    # log scalar result values (job result metrics)
    context.log_result('accuracy', p1 * 2)
    context.log_result('loss', p1 * 3)
    context.set_label('framework', 'sklearn')

    # log various types of artifacts (file, web page, table), will be versioned and visible in the UI
    context.log_artifact('model.txt', body=b'abc is 123', labels={'framework': 'xgboost'})
    context.log_artifact('results.html', body=b'<b> Some HTML <b>', viewer='web-app')

    """

    if global_context.get() and not spec and not event:
        return global_context.get()

    if 'global_mlrun_context' in globals() and not spec and not event:
        return globals().get('global_mlrun_context')

    newspec = {}
    config = environ.get('MLRUN_EXEC_CONFIG')
    if event:
        newspec = event.body

    elif spec:
        newspec = deepcopy(spec)

    elif with_env and config:
        newspec = config

    if isinstance(newspec, (RunObject, RunTemplate)):
        newspec = newspec.to_dict()

    if newspec and not isinstance(newspec, dict):
        newspec = json.loads(newspec)

    if not newspec:
        newspec = {}

    update_in(newspec, 'metadata.name', name, replace=False)
    autocommit = False
    tmp = environ.get('MLRUN_META_TMPFILE')
    out = rundb or mlconf.dbpath or environ.get('MLRUN_DBPATH')
    if out:
        autocommit = True
        logger.info('logging run results to: {}'.format(out))

    ctx = MLClientCtx.from_dict(
        newspec, rundb=out, autocommit=autocommit, tmp=tmp, host=socket.gethostname()
    )
    return ctx


def import_function(url='', secrets=None, db=''):
    """Create function object from DB or local/remote YAML file

    Reading from a file or remote URL (http(s), s3, git, v3io, ..)

    :param url: path/url to function YAML file or
                'db://{project}/{name}[:tag]' when reading from mlrun db
    :param secrets: optional, credentials dict for DB or URL (s3, v3io, ...)
    :param db: optional, mlrun api/db path
    :returns: function object
    """
    if url.startswith('db://'):
        url = url[5:]
        project, name, tag, hash_key = parse_function_uri(url)
        db = get_run_db(db or get_or_set_dburl()).connect(secrets)
        runtime = db.get_function(name, project, tag, hash_key)
        if not runtime:
            raise KeyError('function {}:{} not found in the DB'.format(name, tag))
        return new_function(runtime=runtime)

    url = extend_hub_uri(url)
    runtime = import_function_to_dict(url, secrets)
    return new_function(runtime=runtime)


def import_function_to_dict(url, secrets=None):
    """Load function spec from local/remote YAML file"""
    obj = get_object(url, secrets)
    runtime = yaml.load(obj, Loader=yaml.FullLoader)
    remote = '://' in url

    code = get_in(runtime, 'spec.build.functionSourceCode')
    update_in(runtime, 'metadata.build.code_origin', url)
    cmd = code_file = get_in(runtime, 'spec.command', '')
    if ' ' in cmd:
        code_file = cmd[: cmd.find(' ')]
    if runtime['kind'] in ['', 'local']:
        if code:
            fpath = mktemp('.py')
            code = b64decode(code).decode('utf-8')
            update_in(runtime, 'spec.command', fpath)
            with open(fpath, 'w') as fp:
                fp.write(code)
        elif remote and cmd:
            if cmd.startswith('/'):
                raise ValueError('exec path (spec.command) must be relative')
            url = url[: url.rfind('/') + 1] + code_file
            code = get_object(url, secrets)
            dir = path.dirname(code_file)
            if dir:
                makedirs(dir, exist_ok=True)
            with open(code_file, 'wb') as fp:
                fp.write(code)
        elif cmd:
            if not path.isfile(code_file):
                # look for the file in a relative path to the yaml
                slash = url.rfind('/')
                if slash >= 0 and path.isfile(url[: url.rfind('/') + 1] + code_file):
                    raise ValueError(
                        'exec file spec.command={}'.format(code_file)
                        + ' is relative, change working dir'
                    )
                raise ValueError(
                    'no file in exec path (spec.command={})'.format(code_file)
                )
        else:
            raise ValueError('command or code not specified in function spec')

    return runtime


def new_function(
    name: str = '',
    project: str = '',
    tag: str = '',
    kind: str = '',
    command: str = '',
    image: str = '',
    args: list = None,
    runtime=None,
    mode=None,
    kfp=None,
):
    """Create a new ML function from base properties

    e.g.:
           # define a container based function
           f = new_function(command='job://training.py -v', image='myrepo/image:latest')

           # define a handler function (execute a local function handler)
           f = new_function().run(task, handler=myfunction)

    :param name:     function name
    :param project:  function project (none for 'default')
    :param tag:      function version tag (none for 'latest')

    :param kind:     runtime type (local, job, nuclio, spark, mpijob, dask, ..)
    :param command:  command/url + args (e.g.: training.py --verbose)
    :param image:    container image (start with '.' for default registry)
    :param args:     command line arguments (override the ones in command)
    :param runtime:  runtime (job, nuclio, spark, dask ..) object/dict
                     store runtime specific details and preferences
    :param mode:     runtime mode, e.g. noctx, pass to bypass mlrun
    :param kfp:      reserved, flag indicating running within kubeflow pipeline

    :return: function object
    """
    kind, runtime = _process_runtime(command, runtime, kind)
    command = get_in(runtime, 'spec.command', command)
    name = name or get_in(runtime, 'metadata.name', '')

    if not kind and not command:
        runner = HandlerRuntime()
    else:
        if kind in ['', 'local'] and command:
            runner = LocalRuntime.from_dict(runtime)
        elif kind in RuntimeKinds.all():
            runner = get_runtime_class(kind).from_dict(runtime)
        else:
            raise Exception(
                'unsupported runtime ({}) or missing command, '.format(kind)
                + 'supported runtimes: {}'.format(
                    ','.join(RuntimeKinds.all() + ['local'])
                )
            )

    if not name:
        # todo: regex check for valid name
        if command and kind not in [RuntimeKinds.remote]:
            name, _ = path.splitext(path.basename(command))
        else:
            name = 'mlrun-' + uuid.uuid4().hex[0:6]
    runner.metadata.name = name
    runner.metadata.project = (
        runner.metadata.project or project or mlconf.default_project
    )
    if tag:
        runner.metadata.tag = tag
    if image:
        if kind in ['', 'handler', 'local']:
            raise ValueError(
                'image should only be set with containerized '
                'runtimes (job, mpijob, spark, ..), set kind=..'
            )
        runner.spec.image = image
    if args:
        runner.spec.args = args
    runner.kfp = kfp
    if mode:
        runner.spec.mode = mode
    return runner


def _process_runtime(command, runtime, kind):
    if runtime and hasattr(runtime, 'to_dict'):
        runtime = runtime.to_dict()
    if runtime and isinstance(runtime, dict):
        kind = kind or runtime.get('kind', '')
        command = command or get_in(runtime, 'spec.command', '')
    if '://' in command and command.startswith('http'):
        kind = kind or RuntimeKinds.remote
    if not runtime:
        runtime = {}
    update_in(runtime, 'spec.command', command)
    runtime['kind'] = kind
    if kind != RuntimeKinds.remote:
        parse_command(runtime, command)
    else:
        update_in(runtime, 'spec.function_kind', 'mlrun')
    return kind, runtime


def parse_command(runtime, url):
    idx = url.find('#')
    if idx > -1:
        update_in(runtime, 'spec.image', url[:idx])
        url = url[idx + 1 :]

    if url:
        arg_list = url.split()
        update_in(runtime, 'spec.command', arg_list[0])
        update_in(runtime, 'spec.args', arg_list[1:])


def code_to_function(
    name: str = '',
    project: str = '',
    tag: str = '',
    filename: str = '',
    handler: str = '',
    kind: str = '',
    image: str = None,
    code_output='',
    embed_code=True,
    description='',
    categories: list = None,
    labels: dict = None,
    with_doc=True,
):
    """convert code or notebook to function object with embedded code
    code stored in the function spec and can be refreshed using .with_code()
    eliminate the need to build container images every time we edit the code

    :param name:        function name
    :param project:     function project (none for 'default')
    :param tag:         function tag (none for 'latest')
    :param filename:    blank for current notebook, or path to .py/.ipynb file
    :param handler:     name of function handler (if not main)
    :param kind:        optional, runtime type local, job, dask, mpijob, ..
    :param image:       optional, container image
    :param code_output: save the generated code (from notebook) in that path
    :param embed_code:  embed the source code into the function spec
    :param description: function description
    :param categories:  list of categories (for function marketplace)
    :param labels:      dict of label names and values to tag the function
    :param with_doc:    document the function parameters

    :return:
           function object
    """
    filebase, _ = path.splitext(path.basename(filename))

    def add_name(origin, name=''):
        name = filename or (name + '.ipynb')
        if not origin:
            return name
        return '{}:{}'.format(origin, name)

    def update_meta(fn):
        fn.spec.description = description
        fn.metadata.project = project or mlconf.default_project
        fn.metadata.tag = tag
        fn.metadata.categories = categories
        fn.metadata.labels = labels

    if (
        not embed_code
        and not code_output
        and (not filename or filename.endswith('.ipynb'))
    ):
        raise ValueError(
            'a valid code file must be specified '
            'when not using the embed_code option'
        )

    subkind = kind[kind.find(':') + 1 :] if kind.startswith('nuclio:') else None
    code_origin = add_name(add_code_metadata(filename), name)

    name, spec, code = build_file(
        filename, name=name, handler=handler or 'handler', kind=subkind
    )
    spec_kind = get_in(spec, 'kind', '')
    if spec_kind not in ['', 'Function']:
        kind = spec_kind.lower()

        # if its a nuclio subkind, redo nb parsing
        if kind.startswith('nuclio:'):
            subkind = kind[kind.find(':') + 1 :]
            name, spec, code = build_file(
                filename, name=name, handler=handler or 'handler', kind=subkind
            )

    if code_output:
        if code_output == '.':
            code_output = name + '.py'
        if filename == '' or filename.endswith('.ipynb'):
            with open(code_output, 'w') as fp:
                fp.write(code)
        else:
            raise ValueError('code_output option is only used with notebooks')

    if kind.startswith('nuclio'):
        r = RemoteRuntime()
        r.spec.function_kind = subkind
        if embed_code:
            update_in(spec, 'kind', 'Function')
            r.spec.base_spec = spec
            if with_doc:
                handlers = find_handlers(code)
                r.spec.entry_points = {h['name']: as_func(h) for h in handlers}
        else:
            r.spec.source = filename
            r.spec.function_handler = handler

        if not name:
            raise ValueError('name must be specified')
        r.metadata.name = name
        r.spec.build.code_origin = code_origin
        update_meta(r)
        return r

    if kind is None or kind in ['', 'Function']:
        raise ValueError('please specify the function kind')
    elif kind in ['local']:
        r = LocalRuntime()
    elif kind in RuntimeKinds.all():
        r = get_runtime_class(kind)()
    else:
        raise ValueError('unsupported runtime ({})'.format(kind))

    name, spec, code = build_file(filename, name=name)

    if not name:
        raise ValueError('name must be specified')
    h = get_in(spec, 'spec.handler', '').split(':')
    r.handler = h[0] if len(h) <= 1 else h[1]
    r.metadata = get_in(spec, 'spec.metadata')
    r.metadata.name = name
    r.spec.image = get_in(spec, 'spec.image', image)
    build = r.spec.build
    build.code_origin = code_origin
    build.base_image = get_in(spec, 'spec.build.baseImage')
    build.commands = get_in(spec, 'spec.build.commands')
    if embed_code:
        build.functionSourceCode = get_in(spec, 'spec.build.functionSourceCode')
    else:
        if code_output:
            r.spec.command = code_output
        else:
            r.spec.command = filename

    build.image = get_in(spec, 'spec.build.image')
    build.secret = get_in(spec, 'spec.build.secret')
    if r.kind != 'local':
        r.spec.env = get_in(spec, 'spec.env')
        for vol in get_in(spec, 'spec.volumes', []):
            r.spec.volumes.append(vol.get('volume'))
            r.spec.volume_mounts.append(vol.get('volumeMount'))

    if with_doc:
        handlers = find_handlers(code)
        r.spec.entry_points = {h['name']: as_func(h) for h in handlers}
    r.spec.default_handler = handler
    update_meta(r)
    return r


def run_pipeline(
    pipeline,
    arguments=None,
    project=None,
    experiment=None,
    run=None,
    namespace=None,
    artifact_path=None,
    ops=None,
    url=None,
    ttl=None,
):
    """remote KubeFlow pipeline execution

    Submit a workflow task to KFP via mlrun API service

    :param pipeline   KFP pipeline function or path to .yaml/.zip pipeline file
    :param arguments  pipeline arguments
    :param experiment experiment name
    :param run        optional, run name
    :param namespace  Kubernetes namespace (if not using default)
    :param url        optional, url to mlrun API service
    :param artifact_path  target location/url for mlrun artifacts
    :param ops        additional operators (.apply() to all pipeline functions)
    :param ttl        pipeline ttl in secs (after that the pods will be removed)

    :return kubeflow pipeline id
    """

    remote = not get_k8s_helper(silent=True).is_running_inside_kubernetes_cluster()

    artifact_path = artifact_path or mlconf.artifact_path
    if artifact_path and '{{run.uid}}' in artifact_path:
        artifact_path.replace('{{run.uid}}', '{{workflow.uid}}')
    if artifact_path and '{{run.project}}' in artifact_path:
        if not project:
            raise ValueError(
                'project name must be specified with this'
                + f' artifact_path template {artifact_path}'
            )
        artifact_path.replace('{{run.project}}', project)
    if not artifact_path:
        raise ValueError('artifact path was not specified')

    namespace = namespace or mlconf.namespace
    arguments = arguments or {}

    if remote or url:
        mldb = get_run_db(url).connect()
        if mldb.kind != 'http':
            raise ValueError(
                'run pipeline require access to remote api-service'
                ', please set the dbpath url'
            )
        id = mldb.submit_pipeline(
            pipeline,
            arguments,
            experiment=experiment,
            run=run,
            namespace=namespace,
            ops=ops,
            artifact_path=artifact_path,
        )

    else:
        client = Client(namespace=namespace)
        if isinstance(pipeline, str):
            experiment = client.create_experiment(name=experiment)
            run_result = client.run_pipeline(
                experiment.id, run, pipeline, params=arguments
            )
        else:
            conf = new_pipe_meta(artifact_path, ttl, ops)
            run_result = client.create_run_from_pipeline_func(
                pipeline,
                arguments,
                run_name=run,
                experiment_name=experiment,
                pipeline_conf=conf,
            )

        id = run_result.run_id
    logger.info('Pipeline run id={}, check UI or DB for progress'.format(id))
    return id


def wait_for_pipeline_completion(
    run_id, timeout=60 * 60, expected_statuses: typing.List[str] = None, namespace=None
):
    """Wait for Pipeline status, timeout in sec

    :param run_id:     id of pipelines run
    :param timeout:    wait timeout in sec
    :param expected_statuses:  list of expected statuses, one of [ Succeeded | Failed | Skipped | Error ], by default
                               [ Succeeded ]
    :param namespace:  k8s namespace if not default

    :return kfp run dict
    """
    if expected_statuses is None:
        expected_statuses = [RunStatuses.succeeded]
    namespace = namespace or mlconf.namespace
    remote = not get_k8s_helper(silent=True).is_running_inside_kubernetes_cluster()
    logger.debug(
        f"Waiting for run completion."
        f" run_id: {run_id},"
        f" expected_statuses: {expected_statuses},"
        f" timeout: {timeout},"
        f" remote: {remote},"
        f" namespace: {namespace}"
    )

    if remote:
        mldb = get_run_db().connect()

        def get_pipeline_if_completed(run_id, namespace=namespace):
            resp = mldb.get_pipeline(run_id, namespace=namespace)
            status = resp['run']['status']
            if status not in RunStatuses.stable_statuses():

                # TODO: think of nicer liveness indication and make it re-usable
                # log '.' each retry as a liveness indication
                logger.debug('.')
                raise RuntimeError('pipeline run has not completed yet')

            return resp

        if mldb.kind != 'http':
            raise ValueError(
                'get pipeline require access to remote api-service'
                ', please set the dbpath url'
            )

        resp = retry_until_successful(
            10,
            timeout,
            logger,
            False,
            get_pipeline_if_completed,
            run_id,
            namespace=namespace,
        )
    else:
        client = Client(namespace=namespace)
        resp = client.wait_for_run_completion(run_id, timeout)
        if resp:
            resp = resp.to_dict()

    status = resp['run']['status'] if resp else 'unknown'
    if expected_statuses:
        if status not in expected_statuses:
            raise RuntimeError(f"run status {status} not in expected statuses")

    logger.debug(
        f"Finished waiting for pipeline completion."
        f" run_id: {run_id},"
        f" status: {status},"
        f" namespace: {namespace}"
    )

    return resp


def get_pipeline(run_id, namespace=None):
    """Get Pipeline status

    :param run_id:     id of pipelines run
    :param namespace:  k8s namespace if not default

    :return kfp run dict
    """
    namespace = namespace or mlconf.namespace
    remote = not get_k8s_helper(silent=True).is_running_inside_kubernetes_cluster()
    if remote:
        mldb = get_run_db().connect()
        if mldb.kind != 'http':
            raise ValueError(
                'get pipeline require access to remote api-service'
                ', please set the dbpath url'
            )

        resp = mldb.get_pipeline(run_id, namespace=namespace)

    else:
        client = Client(namespace=namespace)
        resp = client.get_run(run_id)
        if resp:
            resp = resp.to_dict()

    return resp


def list_piplines(
    full=False,
    page_token='',
    page_size=10,
    sort_by='',
    experiment_id=None,
    namespace=None,
):
    """List pipelines"""
    namespace = namespace or mlconf.namespace
    client = Client(namespace=namespace)
    resp = client._run_api.list_runs(
        page_token=page_token, page_size=page_size, sort_by=sort_by
    )
    runs = resp.runs
    if not full and runs:
        runs = []
        for run in resp.runs:
            runs.append(
                {
                    k: str(v)
                    for k, v in run.to_dict().items()
                    if k
                    in [
                        'id',
                        'name',
                        'status',
                        'error',
                        'created_at',
                        'scheduled_at',
                        'finished_at',
                        'description',
                    ]
                }
            )

    return resp.total_size, resp.next_page_token, runs


def as_func(handler):
    ret = clean(handler['return'])
    return FunctionEntrypoint(
        name=handler['name'],
        doc=handler['doc'],
        parameters=[clean(p) for p in handler['params']],
        outputs=[ret] if ret else None,
        lineno=handler['lineno'],
    ).to_dict()


def clean(struct: dict):
    if not struct:
        return None
    if 'default' in struct:
        struct['default'] = py_eval(struct['default'])
    return {k: v for k, v in struct.items() if v or k == 'default'}


def py_eval(data):
    try:
        value = literal_eval(data)
        return value
    except (SyntaxError, ValueError):
        return data


def get_object(url, secrets=None, size=None, offset=0, db=None):
    """get mlrun dataitem body (from path/url)"""
    stores = store_manager.set(secrets, db=db)
    return stores.object(url=url).get(size, offset)


def get_dataitem(url, secrets=None, db=None):
    """get mlrun dataitem object (from path/url)"""
    stores = store_manager.set(secrets, db=db)
    return stores.object(url=url)


def download_object(url, target, secrets=None):
    """download mlrun dataitem (from path/url to target path)"""
    stores = store_manager.set(secrets)
    stores.object(url=url).download(target_path=target)
