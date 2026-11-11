"""Persistent native Direct Link publisher, controlled over authenticated loopback TCP.

Unreal's message bus transports the scene. Only mesh/texture payloads are cached;
the live path never writes or imports .udatasmith files.
"""
import atexit
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import time
import uuid

PUBLISH_TIMEOUT_SECONDS = 900

def default_cache_root(project):
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'BlenderDatasmith' / 'Cache'


class LiveLinkClient:
    def __init__(self, unreal_exe, project, cache_root, name='Blender Live Scene'):
        self.unreal_exe, self.project = str(unreal_exe), str(project)
        from .security import private_directory
        self.input_root = private_directory(Path(cache_root)/'secure-v1')
        self.directory = self.input_root/('session_'+uuid.uuid4().hex)
        self.name, self.token = name, secrets.token_hex(32)
        self.cache_directory=private_directory(self.input_root/'resources')
        self.last_digest=None
        from .cache import ExtractionCache
        self.extraction_cache=ExtractionCache()
        self.process = None; self.port = None; self.log = None
        self.last_result = {}; self.closed = False

    def start(self, timeout=90):
        if not Path(self.unreal_exe).is_file() or not Path(self.project).is_file():
            raise FileNotFoundError('In Unreal use Datasmith > Blender Setup > Use This Unreal Project, then load the connection in Blender preferences')
        from .security import private_directory
        private_directory(self.directory)
        control=self.directory/'control.json'
        control.write_text(json.dumps(dict(token=self.token,name=self.name,owner_pid=os.getpid(),protocol=2,input_root=str(self.input_root))),encoding='utf-8')
        self.log=open(self.directory/'bridge_stdout.log','w',encoding='utf-8')
        args=[self.unreal_exe,self.project,'-run=BlenderDatasmithLive',f'-Control={control}',
              '-Messaging','-UDPMESSAGING_TRANSPORT_ENABLE=1',
              '-UDPMESSAGING_TRANSPORT_UNICAST=127.0.0.1:0',
              '-unattended','-NullRHI','-nosplash','-nop4','-stdout','-FullStdOutLogOutput',f'-abslog={self.directory / "bridge.log"}']
        try:
            self.process=subprocess.Popen(args,stdout=self.log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise RuntimeError(f'Direct Link bridge exited ({self.process.returncode}); see {self.directory / "bridge.log"}')
                try:
                    ready=json.loads((self.directory/'ready.json').read_text(encoding='utf-8-sig'))
                    if ready.get('protocol') != 2:
                        raise RuntimeError('Install matching bridge and Blender add-on versions for secure Direct Link')
                    self.port=int(ready['port']);self.ready=ready
                    self.request('status');atexit.register(self.stop);return self
                except (FileNotFoundError,json.JSONDecodeError):time.sleep(.1)
            raise TimeoutError(f'Direct Link bridge did not start; see {self.directory / "bridge.log"}')
        except Exception:
            self.stop();raise

    def request(self, command, scene=None, timeout=120):
        body=dict(command=command)
        if scene is not None:body['scene']=scene
        data=json.dumps(body,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
        if len(data)>64*1024*1024:raise ValueError('Scene exceeds the 64 MB live manifest limit')
        with socket.create_connection(('127.0.0.1',self.port),timeout=timeout) as stream:
            stream.settimeout(timeout)
            stream.sendall(b'BDL2'+self.token.encode('ascii'))
            if stream.recv(1) != b'\x01':
                raise ConnectionError('Direct Link authentication failed; install matching bridge and Blender add-on versions')
            stream.sendall(struct.pack('!I',len(data))+data)
            def read(n):
                chunks=[]
                while n:
                    block=stream.recv(n)
                    if not block:raise ConnectionError('Direct Link bridge closed the connection')
                    chunks.append(block);n-=len(block)
                return b''.join(chunks)
            size=struct.unpack('!I',read(4))[0]
            if size>1024*1024:raise ValueError('Invalid bridge reply length')
            result=json.loads(read(size).decode('utf-8'))
        if not result.get('ok'):raise RuntimeError(result.get('error','Direct Link request failed'))
        self.last_result=result;return result

    def publish(self, context, bake_materials=False, bake_resolution=1024, bake_samples=16, auto_bake=False):
        from .exporter import build_manifest
        revision=self.last_result.get('revision',0)+1
        staging=self.directory/'input'
        manifest=build_manifest(context,staging,bake_materials=bake_materials,bake_resolution=bake_resolution,bake_samples=bake_samples,binary_meshes=True,auto_bake=auto_bake,cache_directory=self.cache_directory,extraction_cache=self.extraction_cache)
        from .cache import digest
        fingerprint=digest({k:sorted(manifest[k],key=lambda item:item['id']) for k in ('actors','meshes','materials','textures')})
        if fingerprint==self.last_digest:
            response=dict(self.request('status'),unchanged=True,mesh_writes=0,texture_writes=0,material_builds=0)
        else:
            # First-time SDK mesh/texture generation for large scenes can take
            # several minutes. Status/authentication retain their normal bounds.
            response=self.request('publish',manifest,timeout=PUBLISH_TIMEOUT_SECONDS);self.last_digest=fingerprint
        response['cache']=manifest.get('cache',{})
        report=dict(response,warnings=manifest['warnings'],meshes=len(manifest['meshes']),actors=len(manifest['actors']),materials=len(manifest['materials']),transport='Epic DirectLink')
        report['material_translation']=manifest.get('material_translation',[])
        if 'baking' in manifest:report['baking']=manifest['baking']
        (self.directory/'last_publish.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        return report

    def stop(self):
        if self.closed:return
        self.closed=True
        if self.process and self.process.poll() is None:
            try:
                if self.port:self.request('stop',timeout=3)
                self.process.wait(timeout=8)
            except Exception:
                self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
        if self.log:self.log.close()
        for name in ('control.json','ready.json'):
            (self.directory/name).unlink(missing_ok=True)
        self.token = ''
        atexit.unregister(self.stop)


# Blender callbacks operate only on the main thread. Debounce changes so a drag
# results in one snapshot after a pause, rather than rebuilding on every event.
_session=None
_scene=None
_dirty=False
_busy=False
_last_event=0.0
_last_status=0.0
_status='Stopped'
_options={}
_warning_count=0

def status():return _status
def running():return _session is not None

def mark_dirty(scene, depsgraph=None):
    global _dirty,_last_event
    if _session and not _busy and scene==_scene:
        _dirty=True;_last_event=time.monotonic()

def sync_now(context):
    global _busy,_dirty,_status,_warning_count
    if not _session:raise RuntimeError('Start Direct Link first')
    _busy=True;_status='Preparing scene and materials...'
    try:
        result=_session.publish(context,**_options)
        _dirty=False;_status=f'Published {result["revision"]} | {result["connections"]} connection(s)'
        _warning_count=len(result['warnings'])
        if _warning_count:_status+=f' | {_warning_count} warning(s)'
        return result
    finally:_busy=False

def timer():
    global _last_status,_status,_dirty
    import bpy
    if not _session:return None
    try:
        if _session.process.poll() is not None:raise RuntimeError('Background bridge stopped')
        now=time.monotonic()
        if _dirty and bpy.context.scene==_scene and bpy.context.mode=='OBJECT' and now-_last_event>=.8:
            sync_now(bpy.context)
        elif now-_last_status>=5:
            result=_session.request('status',timeout=3);_last_status=now
            _status=f'Published {result["revision"]} | {result["connections"]} connection(s)'
            if _warning_count:_status+=f' | {_warning_count} warning(s)'
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type=='VIEW_3D':area.tag_redraw()
    except (TimeoutError, ConnectionError) as exc:
        _last_status=time.monotonic();_status='Waiting for bridge; last scene retained'
        return 1.0
    except Exception as exc:
        if _session and _session.process.poll() is None:
            _dirty=False;_status='Update failed; last scene retained: '+str(exc)
            print('DATASMITH_DIRECTLINK_ERROR',exc)
            return 1.0
        stop();_status='Stopped: '+str(exc)
        print('DATASMITH_DIRECTLINK_ERROR',exc)
        return None
    return .2

def start(context, unreal_exe, project, cache_root, auto_sync=True, source_name=None, **options):
    global _session,_scene,_options,_status
    import bpy
    stop()
    _scene=context.scene;_options=options
    name=source_name or ('Blender - '+(Path(bpy.data.filepath).stem if bpy.data.filepath else context.scene.name))
    _session=LiveLinkClient(unreal_exe,project,cache_root,name).start()
    try:
        result=sync_now(context)
        if auto_sync:bpy.app.handlers.depsgraph_update_post.append(mark_dirty)
        bpy.app.timers.register(timer,first_interval=.2)
        return result
    except Exception:
        stop();raise

def stop(*args):
    global _session,_scene,_dirty,_status
    import bpy
    if mark_dirty in bpy.app.handlers.depsgraph_update_post:bpy.app.handlers.depsgraph_update_post.remove(mark_dirty)
    if bpy.app.timers.is_registered(timer):bpy.app.timers.unregister(timer)
    if _session:_session.stop()
    _session=None;_scene=None;_dirty=False;_status='Stopped'

