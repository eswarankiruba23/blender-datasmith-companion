"""Independent scene extraction. Epic's compiled commandlet owns Datasmith serialization."""
import bpy
import json
import math
import hashlib
import uuid
import subprocess
import os
import re
from types import SimpleNamespace
from pathlib import Path
from mathutils import Matrix

BASIS = Matrix.Diagonal((1, -1, 1, 1))
CAMERA_LOCAL = Matrix(((0, 1, 0, 0), (0, 0, 1, 0), (-1, 0, 0, 0), (0, 0, 0, 1)))
ID_KEY = "blender_datasmith_uuid"

def identity(block, prefix):
    if block is None: return "mat_default"
    if block.library:
        return prefix + hashlib.sha256((block.library.filepath + '|' + block.name_full).encode()).hexdigest()[:32]
    saved = block.get(ID_KEY)
    if not isinstance(saved, str) or re.fullmatch(r'[0-9a-fA-F]{32}', saved) is None:
        block[ID_KEY] = uuid.uuid4().hex
    return prefix + block[ID_KEY]

def unique_ids(blocks):
    # ID-property pointers are saved/remapped by Blender, unlike names or RAM
    # addresses. The registry therefore follows rename, undo and save/reopen.
    scene = bpy.context.scene
    registry_key = '_blender_datasmith_identity_owners'
    if registry_key not in scene: scene[registry_key] = {}
    owners = scene[registry_key]
    groups = {}
    for block in blocks:
        if not block.library:
            groups.setdefault(identity(block, ''), []).append(block)
    for ident, candidates in groups.items():
        key = candidates[0].bl_rna.identifier + '_' + ident
        owner = owners.get(key)
        if owner not in candidates:
            # Migration of previously exported files without a registry: oldest
            # live datablock is preferable to alphabetic collection order.
            owner = min(candidates, key=lambda b: b.session_uid)
        owners[key] = owner
        for block in candidates:
            if block != owner:
                block[ID_KEY] = uuid.uuid4().hex
                owners[block.bl_rna.identifier + '_' + block[ID_KEY]] = block


def maintain_ids(*args):
    for blocks in (bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.images):
        unique_ids(blocks)


def ordered_uv_names(mesh):
    """Keep Blender's render UV at channel zero, including when it was above eight."""
    names = [uv.name for uv in mesh.uv_layers]
    active = next((uv.name for uv in mesh.uv_layers if uv.active_render), None)
    if active:
        names.remove(active)
        names.insert(0, active)
    return names[:8]


def evaluated_material_sources(mesh, obj, warn):
    """Snapshot evaluated slots before image operations can invalidate evaluated IDs."""
    slots = list(obj.material_slots)
    sources = []
    for index in range(max(len(mesh.materials), len(slots))):
        slot = slots[index] if index < len(slots) else None
        if slot is not None and slot.link == 'OBJECT':
            mat = slot.material
        elif index < len(mesh.materials):
            mat = mesh.materials[index]
        else:
            mat = slot.material if slot else None
        sources.append(mat.original if mat is not None else None)
    if not sources:
        sources = [None]
    max_index = max((poly.material_index for poly in mesh.polygons), default=0)
    if max_index >= len(sources):
        warn(f'{obj.name}: evaluated faces reference missing material slots; exporting explicit default slots')
        sources.extend([None] * (max_index + 1 - len(sources)))
    return sources

def build_manifest(context, staging, selected_only=False, bake_materials=False, bake_resolution=1024, bake_samples=16, binary_meshes=False, auto_bake=False, cache_directory=None, extraction_cache=None):
    if extraction_cache:extraction_cache.begin([selected_only,bake_materials,bake_resolution,bake_samples,binary_meshes,auto_bake],context.scene)
    if not bake_materials and not auto_bake:
        return _build_manifest(context, staging, selected_only, binary_meshes=binary_meshes, extraction_cache=extraction_cache)
    from .baking import BakeSession
    warnings = []
    Path(staging).mkdir(parents=True, exist_ok=True)
    from .cache import BakeCache
    if cache_directory is None:
        from .directlink import default_cache_root
        cache_directory=default_cache_root(__file__)/'resources'
    cache=BakeCache(cache_directory)
    with BakeSession(context, staging, bake_resolution, bake_samples, lambda s: warnings.append(s) if s not in warnings else None) as baker:
        result = _build_manifest(context, staging, selected_only, baker, binary_meshes, auto_bake=auto_bake and not bake_materials, bake_cache=cache, extraction_cache=extraction_cache)
    result['warnings'].extend(warnings)
    result.setdefault('cache',{}).update(bake_hits=cache.hits,bake_misses=cache.misses)
    result['baking'] = dict(resolution=bake_resolution, samples=bake_samples, engine='Cycles '+context.scene.cycles.device, mode='unsupported_only' if auto_bake and not bake_materials else 'all')
    return result


def write_binary_mesh(mesh, uv_names, unit, material_count, path):
    """Bounded per-mesh transfer; avoids millions of Python/JSON corner objects."""
    import numpy as np
    import struct
    nv,nt,nl=len(mesh.vertices),len(mesh.loop_triangles),len(mesh.loops)
    vertices=np.empty((nv,3),dtype='<f4');mesh.vertices.foreach_get('co',vertices.ravel())
    vertices*=np.array([unit,-unit,unit],dtype='<f4')
    tri_loops=np.empty((nt,3),dtype='<i4');mesh.loop_triangles.foreach_get('loops',tri_loops.ravel())
    loop_vertices=np.empty(nl,dtype='<i4');mesh.loops.foreach_get('vertex_index',loop_vertices)
    faces=np.empty((nt,4),dtype='<u4');faces[:,:3]=loop_vertices[tri_loops]
    slots=np.empty(nt,dtype='<i4');mesh.loop_triangles.foreach_get('material_index',slots)
    if np.any(slots < 0) or np.any(slots >= material_count):
        raise ValueError('Evaluated triangle references an unexported material slot')
    faces[:,3]=slots
    collapsed=np.zeros(nt,dtype=bool)
    for a,b in ((0,1),(1,2),(2,0)):
        collapsed|=np.all(vertices[faces[:,a]]==vertices[faces[:,b]],axis=1)
    normals=np.empty((nl,3),dtype='<f4');mesh.corner_normals.foreach_get('vector',normals.ravel());normals[:,1]*=-1
    chunks=[struct.pack('<8sIII',b'BDMESH01',nv,nt,len(uv_names)),vertices.tobytes(),faces.tobytes(),normals[tri_loops].tobytes()]
    for name in uv_names:
        uv=np.empty((nl,2),dtype='<f4');mesh.uv_layers[name].data.foreach_get('uv',uv.ravel());uv[:,1]=1-uv[:,1]
        chunks.append(uv[tri_loops].tobytes())
    fingerprint=hashlib.sha256()
    for chunk in chunks:fingerprint.update(chunk)
    content=fingerprint.hexdigest();path=path.parent/(content[:32]+'.bdmesh')
    written=not path.is_file()
    if written:
        with open(path,'wb') as stream:
            for chunk in chunks:stream.write(chunk)
    return dict(binary_file=str(path.resolve()),content_hash=content,vertex_count=nv,triangle_count=nt,uv_channel_count=len(uv_names),collapsed_triangles=int(collapsed.sum()),used_material_slots=np.unique(faces[:,3]).tolist())


def _build_manifest(context, staging, selected_only=False, baker=None, binary_meshes=False, auto_bake=False, bake_cache=None, extraction_cache=None):
    staging = Path(staging).resolve(); staging.mkdir(parents=True, exist_ok=True)
    for blocks in (bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.images): unique_ids(blocks)
    scene = context.scene
    unit = scene.unit_settings.scale_length * 100.0
    context.view_layer.update()
    depsgraph = context.evaluated_depsgraph_get()
    result = dict(schema=1, blender_version=bpy.app.version_string, units='centimeters', meshes=[], actors=[], materials=[], textures=[], warnings=[])
    materials, textures, meshes = {}, {}, {}
    assessments = {}
    from .cache import Fingerprints
    fingerprints=Fingerprints()
    result['material_translation'] = []
    def warn(message):
        if message not in result['warnings']: result['warnings'].append(message)

    def texture(image, normal=False):
        key = identity(image, 'tex_') + ('_normal' if normal else '_color' if image.colorspace_settings.name == 'sRGB' else '_data')
        if key in textures: return key
        from .material_graph import require_direct_image
        require_direct_image(image)
        # Export pixels from a copy: supports generated/packed images, never changes the original image.
        content=fingerprints.image(image)
        dest=staging/(hashlib.sha256(('pixels-v3|'+key+content).encode()).hexdigest()[:24]+'.png')
        if dest.is_file():
            textures[key]=dict(id=key,label=image.name,file=str(dest.resolve()),srgb=not normal and image.colorspace_settings.name=='sRGB',normal=normal,content_hash=content)
            return key
        clone = image.copy()
        try:
            # Explicitly populate ALL copies before changing their filename.
            # A relinked FILE image can have loaded source pixels but a lazy
            # copy; assigning the output path first leaves that copy bufferless.
            # Compact buffers also avoid boxed floats for 4K/8K textures.
            import numpy as np
            pixels=np.empty(len(image.pixels),dtype=np.float32)
            image.pixels.foreach_get(pixels)
            if normal:pixels[1::4]=1.0-pixels[1::4]
            clone.pixels.foreach_set(pixels)
            clone.filepath_raw = str(dest); clone.file_format = 'PNG'; clone.save()
        finally: bpy.data.images.remove(clone)
        textures[key] = dict(id=key, label=image.name, file=str(dest.resolve()), srgb=not normal and image.colorspace_settings.name == 'sRGB', normal=normal,content_hash=content)
        return key

    def material(mat, uv_names):
        # Material UV-name bindings are mesh dependent; keep names stable for each UV layout.
        suffix = hashlib.sha256(json.dumps(list(uv_names),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:24]
        key = identity(mat, 'mat_') + '_' + suffix
        if key in materials: return key
        data = dict(id=key, label=mat.name if mat else 'Default', base_color=[0.8,0.8,0.8,1], metallic=0, roughness=0.5, specular=0.5, opacity=1, coat=0, coat_roughness=0.03, emission=[0,0,0,1], two_sided=False, alpha_texture=False, maps=[])
        materials[key] = data
        if mat is None: return key
        data['two_sided'] = not mat.use_backface_culling
        from .material_graph import translate_material
        translate_material(mat, data, uv_names, texture, warn, context.scene.render.engine)
        return key

    selected = set(context.selected_objects)
    included = set(selected if selected_only else scene.objects)
    for obj in list(included):
        p = obj.parent
        while p: included.add(p); p = p.parent
    # Evaluated IDs can be invalidated by image/bake operations. Keep original
    # datablocks plus value copies, and evaluate each object only when needed.
    instances = [SimpleNamespace(original=i.object.original, is_instance=i.is_instance, parent=i.parent.original if i.parent else None,
                                 persistent_id=tuple(i.persistent_id), matrix_world=i.matrix_world.copy())
                 for i in depsgraph.object_instances]
    for inst in instances:
        original = inst.original
        if inst.is_instance:
            if inst.parent and inst.parent not in (selected if selected_only else included): continue
        elif original not in included: continue
        transform_only = selected_only and not inst.is_instance and original not in selected
        if original.hide_render and not transform_only: continue
        instance_suffix = '_' + '_'.join(str(i) for i in inst.persistent_id if i != 2147483647) if inst.is_instance else ''
        parent = inst.parent if inst.is_instance and inst.parent else original.parent
        depsgraph = context.evaluated_depsgraph_get()
        obj = original.evaluated_get(depsgraph)
        parent_id = identity(parent, 'obj_') if parent else ''
        actor_id = identity(original, 'obj_') + ('_' + parent_id if inst.is_instance else '') + instance_suffix
        world = inst.matrix_world.copy()
        converted = BASIS @ world @ (CAMERA_LOCAL if not transform_only and obj.type in {'CAMERA','LIGHT'} else BASIS)
        loc, rot, scale = converted.decompose()
        rebuilt = Matrix.LocRotScale(loc, rot, scale)
        if max(abs(converted[r][c]-rebuilt[r][c]) for r in range(4) for c in range(4)) > 1e-4:
            warn(f"{original.name}: sheared transform approximated by TRS; apply transforms before export")
        actor = dict(id=actor_id,label=original.name + instance_suffix,parent=parent_id,layer=','.join(c.name for c in original.users_collection),location=[x*unit for x in loc],rotation=[rot.x,rot.y,rot.z,rot.w],scale=list(scale),type='empty')
        if transform_only:
            result['actors'].append(actor)
            continue
        if obj.type in {'MESH','CURVE','SURFACE','FONT','META'}:
            extraction_key=extraction_cache.key(original,world,actor_id) if extraction_cache else None
            cached=extraction_cache.get(actor_id,extraction_key) if extraction_cache else None
            if cached:
                resource=cached['mesh'];meshes[resource['id']]=resource
                materials.update({m['id']:m for m in cached['materials']});textures.update({t['id']:t for t in cached['textures']})
                result['material_translation'].extend(cached['translation'])
                for message in cached['warnings']:warn(message)
                if baker:
                    for message in cached['bake_warnings']:baker.warn(message)
                actor.update(type='mesh',mesh=resource['id'],materials=cached['actor_materials'] if 'actor_materials' in cached else resource['materials'])
                result['actors'].append(actor);continue
            warning_start=len(result['warnings']);translation_start=len(result['material_translation'])
            bake_warning_start=len(baker.reported_warnings) if baker else 0
            evaluated_mesh = obj.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
            owned_mesh = evaluated_mesh.copy() if evaluated_mesh else None
            source_materials = evaluated_material_sources(owned_mesh, obj, warn) if owned_mesh else [None]
            obj.to_mesh_clear()
            mesh = owned_mesh
            try:
                if not mesh or not mesh.polygons: continue
                baked_mats = None
                bake_slots = [bool(baker)] * len(source_materials)
                if baker:
                    from .material_graph import assess_material
                    slot_assessments = []
                    for index, mat in enumerate(source_materials):
                        key = (mat.as_pointer() if mat else 0, tuple(ordered_uv_names(mesh)))
                        if key not in assessments:
                            assessments[key] = assess_material(mat, key[1], scene.render.engine)
                        assessment = assessments[key]
                        slot_assessments.append(assessment)
                        bake_slots[index] = not assessment.get('unavailable') and (assessment['bake'] or not auto_bake)
                        if assessment.get('unavailable'):
                            warn(f'{original.name} / {mat.name}: baking unavailable; retaining direct fallback: {assessment["unavailable"]}')
                    if any(bake_slots):
                        # One mesh has one UV0 tangent basis. Bake its compatible
                        # slots together rather than misbind direct maps to the atlas.
                        bake_slots = [not a.get('unavailable') for a in slot_assessments]
                    for index, mat in enumerate(source_materials):
                        assessment = slot_assessments[index]
                        reasons = assessment['reasons'] or (['Shares atlas with a material requiring baking'] if bake_slots[index] else [])
                        result['material_translation'].append(dict(object=original.name, material=mat.name if mat else 'Default', mode='baked' if bake_slots[index] else 'direct', reasons=reasons, unavailable=assessment.get('unavailable')))
                if baker and any(bake_slots):
                    bake_sources = [mat if bake_slots[i] else None for i, mat in enumerate(source_materials)]
                    mesh, baked_mats, baked_textures = bake_cache.bake(baker, mesh, original, world, actor_id, bake_sources) if bake_cache else baker.bake_object(mesh, original, world, actor_id, source_materials=bake_sources)
                    for i, m in enumerate(baked_mats):
                        if bake_slots[i]: materials[m['id']] = m
                    for t in baked_textures: textures[t['id']] = t
                mesh.calc_loop_triangles()
                uv_names = ordered_uv_names(mesh)
                if len(mesh.uv_layers)>8: warn(f"{original.name}: UV channels above 8 omitted")
                if baked_mats:
                    # Unbakeable closures retain the explicit diffuse fallback.
                    mats = [m['id'] if bake_slots[i] else material(source_materials[i],uv_names) for i,m in enumerate(baked_mats)]
                else:
                    mats = [material(mat,uv_names) for mat in source_materials]
                base_id = identity(original.data,'mesh_')
                # Modifiers may produce different geometry on objects sharing a mesh.
                mesh_id = base_id + ('_'+identity(original,'') if original.modifiers else '')
                if baked_mats: mesh_id = base_id + '_baked_' + actor_id
                if mesh_id not in meshes and binary_meshes:
                    filename=hashlib.sha256(mesh_id.encode()).hexdigest()[:24]+'.bdmesh'
                    payload=write_binary_mesh(mesh,uv_names,unit,len(mats),staging/filename)
                    meshes[mesh_id]=dict(id=mesh_id,label=original.data.name,materials=mats,**payload)
                if mesh_id not in meshes:
                    vertices = [[v.co.x*unit,-v.co.y*unit,v.co.z*unit] for v in mesh.vertices]
                    triangles, normals, uvs = [], [], [[] for _ in uv_names]
                    for tri in mesh.loop_triangles:
                        # Reflecting Y changes Blender's CCW fronts into Unreal's CW
                        # fronts. Reversing again would turn the mesh inside out.
                        loops = tuple(tri.loops)
                        if not 0 <= tri.material_index < len(mats):
                            raise ValueError(f'{original.name}: evaluated triangle references an unexported material slot')
                        triangles.append([mesh.loops[i].vertex_index for i in loops]+[tri.material_index])
                        for i in loops:
                            n = mesh.corner_normals[i].vector
                            normals.append([n.x,-n.y,n.z])
                            for ch in range(len(uv_names)):
                                uv = mesh.uv_layers[uv_names[ch]].data[i].uv
                                uvs[ch].append([uv.x,1-uv.y])
                    meshes[mesh_id] = dict(id=mesh_id,label=original.data.name,vertices=vertices,triangles=triangles,normals=normals,uvs=uvs,materials=mats)
                actor.update(type='mesh',mesh=mesh_id,materials=mats)
                if extraction_cache and extraction_key:
                    used=[materials[mid] for mid in mats]
                    texture_ids=set()
                    for material_record in used:
                        texture_ids.update(m['texture'] for m in material_record.get('maps',[]))
                        texture_ids.update(n['texture'] for n in material_record.get('graph',{}).get('nodes',[]) if n['op']=='texture')
                    extraction_cache.put(actor_id,extraction_key,meshes[mesh_id],used,[textures[t] for t in texture_ids],result['material_translation'][translation_start:],result['warnings'][warning_start:],baker.reported_warnings[bake_warning_start:] if baker else [])
                    extraction_cache.records[actor_id]['actor_materials']=list(mats)
            finally:
                if baker: baker.clear_temporary_data()
                if owned_mesh: bpy.data.meshes.remove(owned_mesh)
        elif obj.type == 'CAMERA':
            cam = obj.data
            if cam.type != 'PERSP': warn(f"{original.name}: non-perspective camera approximated as perspective")
            if cam.shift_x or cam.shift_y: warn(f"{original.name}: camera lens shift not translated")
            distance = cam.dof.focus_distance
            if cam.dof.focus_object: distance=(cam.dof.focus_object.matrix_world.translation-world.translation).length
            actor.update(type='camera',focal_length=cam.lens,sensor_width=cam.sensor_width,aspect=(scene.render.resolution_x*scene.render.pixel_aspect_x)/(scene.render.resolution_y*scene.render.pixel_aspect_y),focus_distance=distance*unit,fstop=cam.dof.aperture_fstop,dof=cam.dof.use_dof)
        elif obj.type == 'LIGHT':
            light = obj.data
            actor.update(type='light',light_type=light.type,color=list(light.color),intensity=light.energy*683.0,radius=light.shadow_soft_size*unit if hasattr(light,'shadow_soft_size') else 0,attenuation=light.cutoff_distance*unit if light.use_custom_distance else 100000,outer=math.degrees(light.spot_size)/2 if light.type=='SPOT' else 45,inner=math.degrees(light.spot_size)/2*(1-light.spot_blend) if light.type=='SPOT' else 30,width=light.size*unit if light.type=='AREA' else 0,height=(light.size_y if light.shape in {'RECTANGLE','ELLIPSE'} else light.size)*unit if light.type=='AREA' else 0)
            warn('Light intensity uses an approximate 683 lm/W conversion; exposure/render parity is not guaranteed')
        elif obj.type != 'EMPTY': warn(f"{original.name}: {obj.type} exported as an empty actor")
        result['actors'].append(actor)
    if extraction_cache:
        extraction_cache.finish();result['cache']=dict(extraction_hits=extraction_cache.hits,extraction_misses=extraction_cache.misses)
    result['meshes']=list(meshes.values()); result['materials']=list(materials.values()); result['textures']=list(textures.values())
    actor_ids={a['id'] for a in result['actors']}
    for actor in result['actors']:
        if actor.get('parent') and actor['parent'] not in actor_ids:
            warn(f"{actor['label']}: filtered parent omitted; preserving world transform")
            actor['parent']=''
    return result

def export_scene(context, filepath, unreal_exe, project, selected_only=False, bake_materials=False, bake_resolution=1024, bake_samples=16, auto_bake=False):
    out = Path(filepath).resolve().with_suffix('.udatasmith')
    if not Path(unreal_exe).is_file(): raise FileNotFoundError('Set the UnrealEditor-Cmd.exe path in add-on preferences')
    if not Path(project).is_file(): raise FileNotFoundError('Register your existing Unreal project in Datasmith > Blender Setup and load its connection in Blender preferences')
    out.parent.mkdir(parents=True,exist_ok=True)
    staging=out.parent/(out.stem+'_Bridge')
    manifest=build_manifest(context,staging,selected_only,bake_materials,bake_resolution,bake_samples,binary_meshes=True,auto_bake=auto_bake,cache_directory=staging/'resources')
    input_path=staging/'scene.json'
    input_path.write_text(json.dumps(manifest,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    log=out.with_suffix('.export.log')
    args=[str(unreal_exe),str(project),'-run=BlenderDatasmith',f'-Input={input_path}',f'-Output={out}','-unattended','-NullRHI','-nosplash','-nop4','-stdout','-FullStdOutLogOutput',f'-abslog={log}']
    process=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (staging/'process.log').write_text(process.stdout,encoding='utf-8')
    if process.returncode or 'BLENDER_DATASMITH_OK' not in process.stdout or not out.is_file():
        raise RuntimeError(f'Datasmith bridge failed (exit {process.returncode}). See {log}')
    report=dict(output=str(out),blender_version=bpy.app.version_string,meshes=len(manifest['meshes']),actors=len(manifest['actors']),materials=len(manifest['materials']),textures=len(manifest['textures']),warnings=manifest['warnings'])
    if 'baking' in manifest: report['baking'] = manifest['baking']
    report['material_translation'] = manifest.get('material_translation', [])
    out.with_suffix('.report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report
