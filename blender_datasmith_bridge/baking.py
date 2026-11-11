"""Cycles PBR atlas baking on disposable evaluated objects and copied node trees."""
import bpy
import numpy as np
import hashlib
from contextlib import contextmanager
from pathlib import Path
from .material_graph import material_output, specular_from_ior, unsupported_principled, optical_profile, optical_limits

CHANNELS = {'base_color': 'Base Color', 'roughness': 'Roughness',
            'metallic': 'Metallic', 'opacity': 'Alpha', 'emission': 'Emission Color'}
OPTICAL_CHANNELS = {'transmission':'Transmission Weight','cloth':'Sheen Weight','fuzz_color':'Sheen Tint'}


def atlas_coverage(mesh, uv_name, size):
    """Interior texel centers, independent of Cycles' background alpha convention."""
    mesh.calc_loop_triangles()
    uv=np.empty(len(mesh.loops)*2,dtype=np.float32)
    mesh.uv_layers[uv_name].data.foreach_get('uv',uv)
    uv=uv.reshape((-1,2)).astype(np.float64)*size
    indices=np.empty(len(mesh.loop_triangles)*3,dtype=np.int32)
    mesh.loop_triangles.foreach_get('loops',indices)
    indices=indices.reshape((-1,3))
    mask=np.zeros((size,size),dtype=bool)
    def candidates():
        # Most triangles in dense assets contain no atlas texel center. Cull
        # these in bounded NumPy batches before doing per-triangle raster work.
        for start in range(0,len(indices),65536):
            points=uv[indices[start:start+65536]]
            low=np.maximum(np.ceil(points.min(axis=1)-.5),0)
            high=np.minimum(np.floor(points.max(axis=1)-.5)+1,size)
            yield from points[np.all(high>low,axis=1)]
    for a,b,c in candidates():
        low=np.maximum(np.floor(np.minimum(np.minimum(a,b),c)).astype(int),0)
        high=np.minimum(np.ceil(np.maximum(np.maximum(a,b),c)).astype(int),size)
        if np.any(high<=low):continue
        yy,xx=np.mgrid[low[1]:high[1],low[0]:high[0]];x=xx+.5;y=yy+.5
        det=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(det)<1e-10:continue
        u=((b[1]-c[1])*(x-c[0])+(c[0]-b[0])*(y-c[1]))/det
        v=((c[1]-a[1])*(x-c[0])+(a[0]-c[0])*(y-c[1]))/det
        mask[low[1]:high[1],low[0]:high[0]]|=(u>=0)&(v>=0)&(u+v<=1)
    # Exclude UV-island edges where filtering blends with the empty atlas background.
    inner=mask.copy()
    for _ in range(2):
        padded=np.pad(inner,1)
        inner=padded[1:-1,1:-1]&padded[:-2,1:-1]&padded[2:,1:-1]&padded[1:-1,:-2]&padded[1:-1,2:]
    return (inner if inner.any() else mask).ravel()


def active_output(tree, node_type):
    return next((n for n in tree.nodes if n.type == node_type and n.is_active_output), None)


def principled_path(tree, socket, parents=(), depth=0):
    """Follow the actual surface path, including groups and shader pass-through inputs."""
    if depth > 64 or not socket.is_linked:
        raise ValueError('Surface must resolve to a single Principled BSDF')
    link = socket.links[0]; node = link.from_node
    if node.type == 'BSDF_PRINCIPLED':
        return tree, node
    if node.type == 'REROUTE':
        return principled_path(tree, node.inputs[0], parents, depth+1)
    if node.type == 'GROUP' and node.node_tree:
        output = active_output(node.node_tree, 'GROUP_OUTPUT')
        inner = next((s for s in output.inputs if s.identifier == link.from_socket.identifier), None) if output else None
        if inner:
            return principled_path(node.node_tree, inner, parents+((tree, node),), depth+1)
    if node.type == 'GROUP_INPUT' and parents:
        parent_tree, group = parents[-1]
        outer = next((s for s in group.inputs if s.identifier == link.from_socket.identifier), None)
        if outer:
            return principled_path(parent_tree, outer, parents[:-1], depth+1)
    raise ValueError(f'Cannot bake PBR channels from surface node {node.bl_idname}; use a single Principled BSDF (groups are supported)')


def shader_leaves(tree, socket, parents=(), depth=0):
    if depth > 64 or not socket.is_linked: raise ValueError('Missing or excessively nested surface shader')
    link=socket.links[0]; node=link.from_node
    if link.from_socket.type in {'VALUE','RGBA','VECTOR'}:return [(tree,node)]
    if node.type in {'MIX_SHADER','ADD_SHADER'}:
        sockets=node.inputs[1:] if node.type=='MIX_SHADER' else node.inputs[:]
        return [leaf for s in sockets for leaf in shader_leaves(tree,s,parents,depth+1)]
    if node.type=='REROUTE': return shader_leaves(tree,node.inputs[0],parents,depth+1)
    if node.type=='GROUP' and node.node_tree:
        output=active_output(node.node_tree,'GROUP_OUTPUT')
        inner=next((s for s in output.inputs if s.identifier==link.from_socket.identifier),None) if output else None
        if inner: return shader_leaves(node.node_tree,inner,parents+((tree,node),),depth+1)
    if node.type=='GROUP_INPUT' and parents:
        parent_tree,group=parents[-1]
        outer=next((s for s in group.inputs if s.identifier==link.from_socket.identifier),None)
        if outer:return shader_leaves(parent_tree,outer,parents[:-1],depth+1)
    if node.type in {'BSDF_PRINCIPLED','BSDF_DIFFUSE','BSDF_GLOSSY','BSDF_ANISOTROPIC','BSDF_GLASS','BSDF_REFRACTION','BSDF_TRANSPARENT','BSDF_TRANSLUCENT','BSDF_SHEEN','EMISSION','HOLDOUT'}:
        return [(tree,node)]
    raise ValueError(f'Unsupported bake surface: {node.bl_idname}')


def constant_channels(material, engine):
    """Only provably constant Principled inputs; linked/group inputs still bake."""
    if not material or not material.use_nodes:return {}
    output=material_output(material.node_tree,engine)
    if not output:return {}
    leaves=shader_leaves(material.node_tree,output.inputs['Surface'])
    if len(leaves)!=1 or leaves[0][1].type!='BSDF_PRINCIPLED':return {}
    shader=leaves[0][1];result={}
    for channel in ('roughness','metallic','opacity'):
        socket=shader.inputs.get(CHANNELS[channel])
        if socket and not socket.is_linked:result[channel]=float(socket.default_value)
    color=shader.inputs.get('Emission Color');strength=shader.inputs.get('Emission Strength')
    if strength and not strength.is_linked and strength.default_value==0:
        result['emission']=[0,0,0,1]
    elif color and strength and not color.is_linked and not strength.is_linked:
        result['emission']=[float(c*strength.default_value) for c in color.default_value[:3]]+[1]
    normal=shader.inputs.get('Normal')
    if normal and not normal.is_linked:result['normal']=None
    for channel,name in OPTICAL_CHANNELS.items():
        socket=shader.inputs.get(name)
        if socket and not socket.is_linked:
            result[channel]=list(socket.default_value) if channel=='fuzz_color' else float(socket.default_value)
    return result


class BakeSession:
    @contextmanager
    def activate_scene(self):
        window = self.context.window
        old_scene, old_layer = window.scene, window.view_layer
        try:
            window.scene = self.scene
            yield
        finally:
            window.scene = old_scene
            window.view_layer = old_layer

    def __init__(self, context, staging, resolution, samples, warn):
        if context.mode != 'OBJECT':
            raise ValueError('Switch to Object Mode before baking Datasmith materials')
        self.context, self.staging = context, Path(staging).resolve()
        self.reported_warnings=[]
        self.warn=lambda message:(self.reported_warnings.append(message),warn(message))
        self.resolution, self.samples = int(resolution), int(samples)
        self.source_engine = context.scene.render.engine
        self.scene = None
        self.objects, self.meshes, self.materials, self.groups, self.images = [], [], [], [], []

    def __enter__(self):
        self.scene = bpy.data.scenes.new('__DatasmithBake')
        self.scene.render.engine = 'CYCLES'
        # Honor the user's configured Cycles device; the temporary scene must
        # not silently force CPU when GPU rendering is already configured.
        self.scene.cycles.device = self.context.scene.cycles.device
        self.scene.cycles.samples = self.samples
        self.scene.render.bake.use_selected_to_active = False
        self.scene.render.bake.margin = max(2, min(16, self.resolution//128))
        self.scene.render.bake.normal_space = 'TANGENT'
        self.scene.render.bake.normal_r = 'POS_X'
        self.scene.render.bake.normal_g = 'POS_Y'
        self.scene.render.bake.normal_b = 'POS_Z'
        return self

    def __exit__(self, *exc):
        self.clear_temporary_data()
        if self.scene: bpy.data.scenes.remove(self.scene)

    def clear_temporary_data(self):
        # Remove only datablocks owned by this session, including after a failed bake.
        for obj in reversed(self.objects): bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in reversed(self.meshes): bpy.data.meshes.remove(mesh)
        for mat in reversed(self.materials): bpy.data.materials.remove(mat)
        for group in reversed(self.groups): bpy.data.node_groups.remove(group, do_unlink=True)
        for image in reversed(self.images): bpy.data.images.remove(image)
        self.objects.clear(); self.meshes.clear(); self.materials.clear()
        self.groups.clear(); self.images.clear()

    def copy_material(self, source, source_uv=None):
        if source is None or not source.use_nodes:
            mat = bpy.data.materials.new('__DatasmithDefaultBake')
            mat.use_nodes = True
            if source: mat.node_tree.nodes.get('Principled BSDF').inputs['Base Color'].default_value = source.diffuse_color
        else:
            mat = source.copy()
        self.materials.append(mat)
        mat['datasmith_bake_source_label'] = source.name if source else 'Default'
        def copy_groups(tree, ancestors=()):
            if len(ancestors) > 64: raise ValueError('Material group nesting exceeds 64')
            # Normalize implicit color-to-surface emission in disposable trees.
            for link in list(tree.links):
                if link.to_socket.type=='SHADER' and link.from_socket.type in {'VALUE','RGBA','VECTOR'}:
                    source_socket,target_socket=link.from_socket,link.to_socket
                    emission=tree.nodes.new('ShaderNodeEmission')
                    tree.links.new(source_socket,emission.inputs['Color']);tree.links.new(emission.outputs[0],target_socket)
            for node in list(tree.nodes):
                if node.type == 'TEX_IMAGE' and node.image and (node.image.size[0] == 0 or node.image.size[1] == 0):
                    raise ValueError(f'{mat.name}: missing image pixels for {node.image.name}')
                if source_uv:
                    if node.type == 'TEX_IMAGE' and not node.inputs['Vector'].is_linked:
                        uv=tree.nodes.new('ShaderNodeUVMap');uv.uv_map=source_uv
                        tree.links.new(uv.outputs['UV'],node.inputs['Vector'])
                    if node.type == 'TEX_COORD':
                        links=list(node.outputs['UV'].links)
                        if links:
                            uv=tree.nodes.new('ShaderNodeUVMap');uv.uv_map=source_uv
                            for link in links:tree.links.new(uv.outputs['UV'],link.to_socket)
                    if node.type in {'NORMAL_MAP','TANGENT'} and hasattr(node,'uv_map') and not node.uv_map:node.uv_map=source_uv
                if node.type == 'GROUP' and node.node_tree:
                    original = node.node_tree
                    if original.as_pointer() in ancestors: raise ValueError('Recursive material group')
                    clone = original.copy(); self.groups.append(clone); node.node_tree = clone
                    copy_groups(clone, ancestors+(original.as_pointer(),))
        copy_groups(mat.node_tree)
        output=material_output(mat.node_tree,self.source_engine)
        if output:
            # Remove only disposable alternative outputs, then explicitly make
            # the selected source surface the Cycles bake output.
            for other in list(mat.node_tree.nodes):
                if other.type=='OUTPUT_MATERIAL' and other!=output: mat.node_tree.nodes.remove(other)
            output.target='ALL';output.is_active_output=True
        return mat

    def prepare_channel(self, mat, channel, image):
        root = mat.node_tree
        output = active_output(root, 'OUTPUT_MATERIAL')
        if not output: raise ValueError(f'{mat.name}: missing material output')
        label = mat.get('datasmith_bake_source_label', mat.name)
        if output.inputs['Volume'].is_linked: self.warn(f'{label}: volume is not represented by PBR baking')
        if output.inputs['Displacement'].is_linked: self.warn(f'{label}: shader displacement is not baked; use evaluated geometry instead')
        leaves = shader_leaves(root, output.inputs['Surface'])
        if len(leaves)>1: self.warn('Shader mixtures are reduced to weighted PBR maps; layered and view-dependent BRDF behavior is approximate')
        seen=set()
        for tree,shader in leaves:
            if shader.as_pointer() in seen: continue
            seen.add(shader.as_pointer())
            specular=shader.inputs.get('Specular IOR Level')
            if specular and specular.is_linked: self.warn('Linked specular IOR is not baked; Unreal uses specular 0.5')
            for name in (*unsupported_principled(shader), 'Coat Weight'):
                s = shader.inputs.get(name)
                if s and (s.is_linked or name != 'Coat Weight' or s.default_value > 0):
                    self.warn(f'Baked material: {name} is outside the baked PBR subset')
            if shader.type=='BSDF_TRANSLUCENT':
                self.warn('Diffuse translucency is not equivalent to thin glass and remains a surface approximation')
            if channel == 'normal': continue
            emit = tree.nodes.new('ShaderNodeEmission')
            source = None; value=0.0
            if shader.type=='BSDF_PRINCIPLED': source=shader.inputs.get((CHANNELS|OPTICAL_CHANNELS)[channel])
            elif channel=='transmission': value=1.0 if shader.type in {'BSDF_GLASS','BSDF_REFRACTION'} else 0.0
            elif channel=='cloth': value=1.0 if shader.type=='BSDF_SHEEN' else 0.0
            elif channel=='fuzz_color':
                if shader.type=='BSDF_SHEEN':source=shader.inputs.get('Color')
            elif channel=='base_color' and shader.type not in {'EMISSION','HOLDOUT'}: source=shader.inputs.get('Color')
            elif channel=='roughness':
                source=shader.inputs.get('Roughness');value=.5
            elif channel=='metallic': value=1.0 if shader.type in {'BSDF_GLOSSY','BSDF_ANISOTROPIC'} else 0.0
            elif channel=='opacity': value=0.0 if shader.type in {'BSDF_TRANSPARENT','HOLDOUT'} else 1.0
            elif channel=='emission' and shader.type=='EMISSION':source=shader.inputs['Color']
            if source and source.is_linked: tree.links.new(source.links[0].from_socket, emit.inputs['Color'])
            else:
                if source: value=source.default_value
                emit.inputs['Color'].default_value=tuple(value) if hasattr(value,'__len__') else (value,value,value,1)
            if channel=='emission':
                strength=shader.inputs.get('Emission Strength' if shader.type=='BSDF_PRINCIPLED' else 'Strength') if shader.type in {'BSDF_PRINCIPLED','EMISSION'} else None
                if strength:
                    if strength.is_linked:tree.links.new(strength.links[0].from_socket,emit.inputs['Strength'])
                    else:emit.inputs['Strength'].default_value=strength.default_value
            if channel=='fuzz_color' and shader.type=='BSDF_PRINCIPLED':
                # Premultiply by actual sheen weight before Mix/Add Shader
                # combines closures. Non-sheen branches must contribute zero.
                strength=shader.inputs.get('Sheen Weight')
                if strength and strength.is_linked:tree.links.new(strength.links[0].from_socket,emit.inputs['Strength'])
                else:emit.inputs['Strength'].default_value=float(strength.default_value) if strength else 0
            for socket in shader.outputs:
                for link in list(socket.links):tree.links.new(emit.outputs[0],link.to_socket)
        target = root.nodes.new('ShaderNodeTexImage'); target.image = image
        for node in root.nodes: node.select = False
        target.select = True; root.nodes.active = target

    def bake_object(self, evaluated_mesh, source, world, actor_id, source_materials=None):
        mesh = evaluated_mesh.copy(); self.meshes.append(mesh)
        if len(mesh.uv_layers) >= 8:
            raise ValueError(f'{source.name}: baking needs a free UV channel (maximum seven source UV maps)')
        original_render_uv = next((u.name for u in mesh.uv_layers if u.active_render), None)
        obj = bpy.data.objects.new('__DatasmithBake_'+source.name, mesh); self.objects.append(obj)
        self.scene.collection.objects.link(obj); obj.matrix_world = world
        obj.color = source.color; obj.pass_index = source.pass_index
        sources = list(source_materials) if source_materials is not None else (list(mesh.materials) or [None])
        constants=[constant_channels(mat,self.source_engine) for mat in sources]
        profiles=[optical_profile(mat,self.source_engine) for mat in sources]
        for profile in profiles:
            for message in optical_limits(profile):self.warn(message)
        optical=set()
        if any(p['shading_model']=='glass' for p in profiles):optical.add('transmission')
        if any(p['shading_model']=='cloth' for p in profiles):optical.update(('cloth','fuzz_color'))
        skipped=set.intersection(*(set(c) for c in constants)) if constants else set()
        slot_indices = [p.material_index for p in mesh.polygons]
        if any(index < 0 or index >= len(sources) for index in slot_indices):
            raise ValueError(f'{source.name}: evaluated bake face references an unexported material slot')
        layer = self.scene.view_layers[0]
        # Blender 4.5 can retain empty object slots after the previous temporary
        # bake object is removed. Refresh membership before selecting the next.
        layer.update()
        for other in layer.objects:
            if other is None:continue
            other.select_set(False, view_layer=layer)
            other.hide_render = other != obj
        obj.select_set(True, view_layer=layer); layer.objects.active = obj
        bake_uv = mesh.uv_layers.new(name='DatasmithBakeUV')
        bake_uv_name = bake_uv.name
        mesh.uv_layers.active_index = len(mesh.uv_layers)-1
        with self.activate_scene(), self.context.temp_override(scene=self.scene, view_layer=layer, object=obj, active_object=obj, selected_objects=[obj], selected_editable_objects=[obj]):
            try:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                # The bake target is square. Blender otherwise takes aspect
                # correction from an active SOURCE image and can squash the
                # entire new atlas into a sub-texel strip (e.g. woven carpets).
                bpy.ops.uv.smart_project(angle_limit=1.1519173, island_margin=0.03, correct_aspect=False)
            finally:
                if obj.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
            # Tangent normals must be baked in the atlas basis. Source shader UV
            # references are made explicit in disposable material copies.
            mesh.uv_layers[bake_uv_name].active_render = True
            coverage=atlas_coverage(mesh,bake_uv_name,self.resolution)
            if not np.any(coverage):
                # Dense foliage can pack into islands smaller than a texel when
                # the ordinary spacing consumes the atlas. Retry the disposable
                # UV layer with less spacing, never accept an empty texture.
                try:
                    bpy.ops.object.mode_set(mode='EDIT')
                    bpy.ops.mesh.select_all(action='SELECT')
                    bpy.ops.uv.smart_project(angle_limit=1.1519173, island_margin=0.001, correct_aspect=False)
                finally:
                    if obj.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
                coverage=atlas_coverage(mesh,bake_uv_name,self.resolution)
                if not np.any(coverage):
                    raise RuntimeError(f'{source.name}: no atlas texel coverage after tighter UV packing; increase bake resolution or simplify the mesh')
                self.warn(f'{source.name}: atlas needed tighter UV packing; inspect foliage edges and use a higher bake resolution for production')
            maps, textures = [], []
            for channel in (*CHANNELS, 'normal', *sorted(optical)):
                if channel in skipped:continue
                image = bpy.data.images.new('__Datasmith_'+channel, width=self.resolution, height=self.resolution, alpha=True, float_buffer=True)
                self.images.append(image)
                image.generated_color = (0,0,0,0)
                image.colorspace_settings.name = 'sRGB' if channel=='base_color' else 'Non-Color'
                mesh.materials.clear()
                for source_mat in sources:
                    mat = self.copy_material(source_mat, original_render_uv)
                    try: self.prepare_channel(mat, channel, image)
                    except ValueError as exc: raise ValueError(f'{source.name} / {source_mat.name if source_mat else "Default"}: {exc}') from exc
                    mesh.materials.append(mat)
                for poly, index in zip(mesh.polygons, slot_indices): poly.material_index = index
                layer.update()
                print(f'DATASMITH_BAKE {source.name} {channel} {self.resolution}', flush=True)
                status = bpy.ops.object.bake(type='NORMAL' if channel == 'normal' else 'EMIT', use_clear=True, uv_layer=bake_uv_name)
                if 'FINISHED' not in status: raise RuntimeError(f'Cycles bake failed: {source.name}/{channel}')
                pixels = np.empty(self.resolution*self.resolution*4, dtype=np.float32)
                image.pixels.foreach_get(pixels); pixels = pixels.reshape((-1,4))
                covered = coverage
                if not np.any(covered): raise RuntimeError(f'Empty bake: {source.name}/{channel}')
                if not np.all(np.isfinite(pixels)): raise RuntimeError(f'Non-finite bake pixels: {source.name}/{channel}')
                if channel == 'opacity' and float(pixels[covered,0].min()) >= 0.999:
                    continue  # An opacity connection makes Unreal infer translucency.
                if channel == 'normal':
                    pixels[:,1] = 1.0-pixels[:,1]  # OpenGL to Unreal DirectX normal convention.
                    image.pixels.foreach_set(pixels.ravel())
                key = f'tex_baked_{actor_id}_{channel}'
                # Actor instance IDs can exceed 100 characters. Keep logical IDs
                # stable but never use them as physical PNG filenames on Windows.
                extension='.exr' if channel=='emission' else '.png'
                path = self.staging/(hashlib.sha256((key+getattr(self,'content_key','')).encode()).hexdigest()[:24]+extension)
                if len(str(path.resolve())) >= 250:
                    raise ValueError('Texture path is too long for Blender image saving. Choose a shorter export or Direct Link cache folder.')
                image.filepath_raw = str(path); image.file_format = 'OPEN_EXR' if channel=='emission' else 'PNG'; image.save()
                textures.append(dict(id=key,label=source.name+'_'+channel,file=str(path.resolve()),srgb=channel=='base_color',normal=channel=='normal'))
                maps.append(dict(kind=channel,texture=key,uv=0,channel=0))
        materials = []
        for i, source_mat in enumerate(sources):
            specular=0.5
            if source_mat and source_mat.node_tree:
                output=material_output(source_mat.node_tree,self.source_engine)
                leaves=shader_leaves(source_mat.node_tree,output.inputs['Surface'])
                if len(leaves)==1:
                    socket=leaves[0][1].inputs.get('Specular IOR Level')
                    ior=leaves[0][1].inputs.get('IOR')
                    if socket and ior and not socket.is_linked and not ior.is_linked:
                        value=specular_from_ior(ior.default_value,socket.default_value)
                        if value>1:self.warn('Baked material: IOR reflectance exceeds standard Unreal Specular range; clamped')
                        specular=min(1,max(0,value))
            materials.append(dict(id=f'mat_baked_{actor_id}_{i}',label=(source_mat.name if source_mat else 'Default')+'_Baked',base_color=[1,1,1,1],metallic=0,roughness=0.5,specular=specular,opacity=1,coat=0,coat_roughness=0.03,emission=[0,0,0,1],two_sided=not source_mat.use_backface_culling if source_mat else True,alpha_texture=any(m['kind']=='opacity' for m in maps),maps=maps))
            materials[-1].update(profiles[i])
            allowed=set(CHANNELS)|{'normal'}
            if profiles[i]['shading_model']=='glass':allowed.add('transmission')
            if profiles[i]['shading_model']=='cloth':allowed.update(('cloth','fuzz_color'))
            materials[-1]['maps']=[m for m in maps if m['kind'] in allowed]
            materials[-1]['fuzz_premultiplied']=profiles[i]['shading_model']=='cloth' and any(m['kind']=='fuzz_color' for m in maps)
            for channel in skipped-{'normal'}:materials[-1][channel]=constants[i][channel]
            if materials[-1]['opacity']<.999:materials[-1]['alpha_texture']=True
        self.warn('Baked materials use object-specific UV atlases; mesh instancing is expanded and procedural controls are not preserved')
        return mesh, materials, textures

