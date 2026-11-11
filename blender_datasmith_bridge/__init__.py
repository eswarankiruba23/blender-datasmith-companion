bl_info = {"name": "Unreal-powered Datasmith Bridge", "author": "Blender Datasmith Bridge contributors", "version": (0, 10, 0), "blender": (4, 5, 0), "location": "File > Export > Unreal Datasmith; Viewport > Datasmith", "category": "Import-Export"}

import bpy
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty, PointerProperty
from pathlib import Path
from bpy_extras.io_utils import ExportHelper
from .exporter import export_scene

class BDPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__
    unreal_exe: StringProperty(name="UnrealEditor-Cmd.exe", subtype='FILE_PATH')
    bridge_project: StringProperty(name="Your Unreal project", subtype='FILE_PATH', description="Existing Unreal project with Blender Datasmith Bridge enabled")
    live_cache: StringProperty(name="Direct Link cache", subtype='DIR_PATH', description="Optional location for live mesh and texture payloads")
    def draw(self, context):
        self.layout.operator('blender_datasmith.use_unreal_connection')
        self.layout.prop(self, "unreal_exe")
        self.layout.prop(self, "bridge_project")
        self.layout.prop(self, "live_cache")
        self.layout.label(text="In Unreal: Datasmith > Blender Setup > Use This Unreal Project.")

class BDUseUnrealConnection(bpy.types.Operator):
    bl_idname = 'blender_datasmith.use_unreal_connection'
    bl_label = 'Use connection from Unreal'
    bl_description = 'Connect to the existing project registered by Unreal; does not start another program'
    def execute(self, context):
        from .connection import configure_preferences
        try:
            from . import directlink
            if directlink.running():
                raise ValueError('Stop Direct Link before changing the project connection')
            configure_preferences(context.preferences.addons[__package__].preferences)
        except Exception as exc:
            self.report({'ERROR'}, f'Use Datasmith > Blender Setup in Unreal. {exc}')
            return {'CANCELLED'}
        self.report({'INFO'}, 'Connection configured. Save Preferences to keep it.')
        return {'FINISHED'}

class BDExport(bpy.types.Operator, ExportHelper):
    bl_idname = "export_scene.blender_datasmith"
    bl_label = "Export Unreal Datasmith"
    filename_ext = ".udatasmith"
    filter_glob: StringProperty(default="*.udatasmith", options={'HIDDEN'})
    selected_only: BoolProperty(name="Selected objects only", default=False)
    bake_materials: BoolProperty(name="Bake complex materials (Cycles)", description="Bake nested Principled materials and procedural inputs to per-object PBR textures; takes longer and expands instances", default=False)
    auto_bake: BoolProperty(name="Bake unsupported materials automatically", description="Keep supported objects direct; bake objects with unsupported surface nodes instead of flat fallbacks. Slower; glass and layered effects remain approximate", default=True)
    bake_resolution: EnumProperty(name="Bake resolution", items=[(str(n),str(n)+' px','') for n in (256,512,1024,2048,4096)], default='1024')
    bake_samples: IntProperty(name="Bake samples", default=16, min=1, max=256)
    def draw(self, context):
        self.layout.prop(self, 'selected_only')
        self.layout.prop(self, 'bake_materials')
        if not self.bake_materials:self.layout.prop(self, 'auto_bake')
        row = self.layout.column(); row.enabled = self.bake_materials or self.auto_bake
        row.prop(self, 'bake_resolution'); row.prop(self, 'bake_samples')
        if self.bake_materials: self.layout.label(text='PBR appearance textures; shader mixtures are approximate.')
        elif self.auto_bake:self.layout.label(text='Unsupported objects bake to textures; this takes longer.')
        else:self.layout.label(text='Direct only: unsupported nodes use plain defaults.')
    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        try:
            from .connection import ensure_connection
            exe, project = ensure_connection(prefs)
            result = export_scene(context, self.filepath, exe, project, self.selected_only, self.bake_materials, int(self.bake_resolution), self.bake_samples, auto_bake=self.auto_bake)
        except Exception as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}
        self.report({'WARNING'} if result['warnings'] else {'INFO'}, f"Datasmith exported; {len(result['warnings'])} warnings. See adjacent .report.json")
        return {'FINISHED'}

def menu(self, context):
    self.layout.operator(BDExport.bl_idname, text="Unreal Datasmith (.udatasmith)")

class BDLiveSettings(bpy.types.PropertyGroup):
    auto_sync: BoolProperty(name='Auto Sync',default=True,description='Publish changes after a short pause in Object Mode')
    bake_materials: BoolProperty(name='Bake complex materials',default=False,description='Rebake materials on every update; can be slow')
    auto_bake: BoolProperty(name='Bake unsupported materials automatically',default=True,description='Bake unsupported objects on each update; keeps supported objects direct. Can be slow on large scenes')
    bake_resolution: EnumProperty(name='Bake resolution',items=[(str(n),str(n)+' px','') for n in (256,512,1024,2048,4096)],default='1024')
    bake_samples: IntProperty(name='Bake samples',default=16,min=1,max=256)

class BDLiveStart(bpy.types.Operator):
    bl_idname='blender_datasmith.live_start'
    bl_label='Start Direct Link'
    @classmethod
    def poll(cls,context):return context.mode=='OBJECT'
    def execute(self,context):
        from . import directlink
        prefs=context.preferences.addons[__package__].preferences;opts=context.scene.blender_datasmith_live
        try:
            from .connection import ensure_connection
            exe, project = ensure_connection(prefs)
            cache=bpy.path.abspath(prefs.live_cache) if prefs.live_cache else str(directlink.default_cache_root(project))
            directlink.start(context,exe,project,cache,auto_sync=opts.auto_sync,bake_materials=opts.bake_materials,bake_resolution=int(opts.bake_resolution),bake_samples=opts.bake_samples,auto_bake=opts.auto_bake)
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
        self.report({'INFO'},'Direct Link source ready. Connect in Unreal using Datasmith Direct Link Import.');return {'FINISHED'}

class BDLiveSync(bpy.types.Operator):
    bl_idname='blender_datasmith.live_sync'
    bl_label='Sync Now'
    @classmethod
    def poll(cls,context):
        from . import directlink
        return directlink.running() and context.mode=='OBJECT'
    def execute(self,context):
        from . import directlink
        try:directlink.sync_now(context)
        except Exception as exc:self.report({'ERROR'},str(exc));return {'CANCELLED'}
        return {'FINISHED'}

class BDLiveStop(bpy.types.Operator):
    bl_idname='blender_datasmith.live_stop'
    bl_label='Stop Direct Link'
    def execute(self,context):
        from . import directlink
        directlink.stop();return {'FINISHED'}

class BDLivePanel(bpy.types.Panel):
    bl_label='Unreal Datasmith Direct Link'
    bl_idname='VIEW3D_PT_blender_datasmith_live'
    bl_space_type='VIEW_3D';bl_region_type='UI';bl_category='Datasmith'
    def draw(self,context):
        from . import directlink
        opts=context.scene.blender_datasmith_live;layout=self.layout
        settings=layout.column();settings.enabled=not directlink.running()
        settings.prop(opts,'auto_sync');settings.prop(opts,'bake_materials')
        if not opts.bake_materials:settings.prop(opts,'auto_bake')
        if opts.bake_materials or opts.auto_bake:
            settings.prop(opts,'bake_resolution');settings.prop(opts,'bake_samples')
            settings.label(text='Baking can pause Blender during each update.')
        else:settings.label(text='Direct only: unsupported nodes use plain defaults.')
        if directlink.running():
            row=layout.row();row.operator(BDLiveSync.bl_idname);row.operator(BDLiveStop.bl_idname)
        else:layout.operator(BDLiveStart.bl_idname)
        layout.label(text=directlink.status())
        if context.mode!='OBJECT':layout.label(text='Return to Object Mode to synchronize edits.')
        layout.label(text='In Unreal: Direct Link Import, then Auto Reimport.')
        layout.label(text='Same PC only. Use trusted local applications.')

def register():
    from . import directlink, exporter
    bpy.app.handlers.persistent(exporter.maintain_ids)
    if exporter.maintain_ids not in bpy.app.handlers.save_pre:bpy.app.handlers.save_pre.append(exporter.maintain_ids)
    for cls in (BDPreferences, BDUseUnrealConnection, BDExport, BDLiveSettings, BDLiveStart, BDLiveSync, BDLiveStop, BDLivePanel): bpy.utils.register_class(cls)
    bpy.types.Scene.blender_datasmith_live=PointerProperty(type=BDLiveSettings)
    bpy.app.handlers.persistent(directlink.stop)
    if directlink.stop not in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.append(directlink.stop)
    bpy.types.TOPBAR_MT_file_export.append(menu)

def unregister():
    from . import directlink, exporter
    if exporter.maintain_ids in bpy.app.handlers.save_pre:bpy.app.handlers.save_pre.remove(exporter.maintain_ids)
    directlink.stop()
    if directlink.stop in bpy.app.handlers.load_pre:bpy.app.handlers.load_pre.remove(directlink.stop)
    del bpy.types.Scene.blender_datasmith_live
    bpy.types.TOPBAR_MT_file_export.remove(menu)
    for cls in (BDLivePanel, BDLiveStop, BDLiveSync, BDLiveStart, BDLiveSettings, BDExport, BDUseUnrealConnection, BDPreferences): bpy.utils.unregister_class(cls)
