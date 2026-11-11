"""Read the user's explicitly registered Unreal project, without launching anything."""
import json
import os
from pathlib import Path
from .security import local_path


def read_registered_connection():
    config = local_path(Path(os.environ['LOCALAPPDATA']) / 'BlenderDatasmith' / 'StockBridge' / 'connection.json')
    if not config.is_file():
        raise ValueError('In Unreal, open Datasmith > Blender Setup and click Use This Unreal Project')
    if config.stat().st_size > 16384:
        raise ValueError('Unexpected connection file size')
    data = json.loads(config.read_text(encoding='utf-8-sig'))
    if data.get('schema') != 2 or data.get('project_mode') != 'existing_project':
        raise ValueError('Register your existing project using Unreal bridge 0.10.0')
    if data.get('bridge_version') != '0.10.0':
        raise ValueError('Install matching 0.10.0 Unreal bridge and Blender companion versions')
    exe, project = local_path(data['unreal_exe']), local_path(data['bridge_project'])
    if exe.name.lower() != 'unrealeditor-cmd.exe' or not exe.is_file():
        raise ValueError('The registered Unreal executable is unavailable; register the project again')
    if project.suffix.lower() != '.uproject' or not project.is_file():
        raise ValueError('The registered Unreal project was moved or removed; register it again')
    if project.name.lower() == 'blenderdatasmithhelper.uproject':
        raise ValueError('Register your actual Unreal project, not an older generated helper')
    return str(exe), str(project)


def configure_preferences(prefs):
    exe, project = read_registered_connection()
    prefs.unreal_exe, prefs.bridge_project = exe, project
    return exe, project


def ensure_connection(prefs):
    # Recover empty/stale preferences only from a project explicitly registered in Unreal.
    # Never silently switch a valid, manually selected project to a different project.
    import bpy
    exe, project = Path(bpy.path.abspath(prefs.unreal_exe)), Path(bpy.path.abspath(prefs.bridge_project))
    if (exe.is_file() and exe.name.lower() == 'unrealeditor-cmd.exe' and
            project.is_file() and project.suffix.lower() == '.uproject' and
            project.name.lower() != 'blenderdatasmithhelper.uproject'):
        return str(exe), str(project)
    return configure_preferences(prefs)
