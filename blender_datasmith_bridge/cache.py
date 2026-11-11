"""Content fingerprints and reusable bake atlases. No live bpy access off-thread."""
import bpy, hashlib, json, copy
from pathlib import Path
import numpy as np
VERSION='0.7.0'

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def array(collection, prop, width, dtype=np.float32):
    result=np.empty(len(collection)*width,dtype=dtype)
    collection.foreach_get(prop,result)
    return result

def geometry_hash(mesh):
    h=hashlib.sha256()
    for c,p,w,t in [(mesh.vertices,'co',3,np.float32),(mesh.loops,'vertex_index',1,np.int32),
                    (mesh.polygons,'loop_total',1,np.int32),(mesh.polygons,'material_index',1,np.int32),
                    (mesh.corner_normals,'vector',3,np.float32)]:
        a=array(c,p,w,t);h.update(len(a).to_bytes(8,'little'));h.update(a.tobytes())
    for uv in mesh.uv_layers:
        h.update(json.dumps([uv.name,uv.active_render]).encode());h.update(array(uv.data,'uv',2).tobytes())
    # Arbitrary named attributes can feed shaders, including vertex colors.
    for attr in sorted(mesh.attributes,key=lambda item:item.name):
        h.update(json.dumps([attr.name,attr.data_type,attr.domain]).encode())
        prop,width={'FLOAT':('value',1),'INT':('value',1),'BOOLEAN':('value',1),
                    'FLOAT_VECTOR':('vector',3),'FLOAT_COLOR':('color',4),'BYTE_COLOR':('color',4),
                    'FLOAT2':('vector',2)}.get(attr.data_type,(None,0))
        if prop:
            try:h.update(array(attr.data,prop,width,np.int32 if attr.data_type in {'INT','BOOLEAN'} else np.float32).tobytes())
            except (TypeError,AttributeError):h.update(str([str(x) for x in attr.data]).encode())
    return h.hexdigest()

class Fingerprints:
    def __init__(self):self.images={};self.materials={}
    def image(self,image):
        key=image.as_pointer()
        if key not in self.images:
            h=hashlib.sha256()
            h.update(json.dumps([image.source,list(image.size),image.colorspace_settings.name,image.alpha_mode]).encode())
            # Pixel bytes catch edits without renaming or timestamp changes.
            pixels=np.empty(len(image.pixels),dtype=np.float32);image.pixels.foreach_get(pixels);h.update(pixels.tobytes())
            if image.source=='TILED':
                # Tile set and external tile content are additional dependencies.
                for tile in image.tiles:
                    h.update(str(tile.number).encode())
                    path=Path(bpy.path.abspath(image.filepath.replace('<UDIM>',str(tile.number))))
                    if path.is_file():
                        with path.open('rb') as f:
                            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
            self.images[key]=h.hexdigest()
        return self.images[key]
    def material(self,mat):
        if mat is None:return 'default',False
        if mat.as_pointer() in self.materials:return self.materials[mat.as_pointer()]
        world=False
        def tree_value(tree,ancestors=()):
            nonlocal world
            if tree.as_pointer() in ancestors:raise ValueError('Recursive shader dependency')
            nodes=[]
            for node in tree.nodes:
                if node.type=='TEX_COORD' and any(o.is_linked and o.name not in {'UV','Generated'} for o in node.outputs):world=True
                if node.type in {'OBJECT_INFO','NEW_GEOMETRY','ATTRIBUTE','TEX_ENVIRONMENT','TEX_SKY','LAYER_WEIGHT','FRESNEL','CAMERA'}:world=True
                props={}
                for prop in node.bl_rna.properties:
                    name=prop.identifier
                    if name in {'rna_type','name','label','location','dimensions','width','height','select','parent','inputs','outputs','internal_links'}:continue
                    try:
                        value=getattr(node,name)
                        if prop.type in {'BOOLEAN','INT','FLOAT','STRING','ENUM'}:
                            props[name]=list(value) if getattr(prop,'is_array',False) else value
                        elif name=='image' and value:props[name]=self.image(value)
                        elif name=='node_tree' and value:props[name]=tree_value(value,ancestors+(tree.as_pointer(),))
                        elif name=='object' and value:props[name]=[value.name_full,[list(r) for r in value.matrix_world]];world=True
                    except (AttributeError,TypeError):pass
                if hasattr(node,'color_ramp'):
                    ramp=node.color_ramp;props['ramp']=[ramp.interpolation,ramp.color_mode,ramp.hue_interpolation,[(x.position,list(x.color)) for x in ramp.elements]]
                if hasattr(node,'mapping'):
                    mapping=node.mapping;props['curves']=[[(list(pt.location),pt.handle_type) for pt in curve.points] for curve in mapping.curves]
                sockets=[]
                for socket in (*node.inputs,*node.outputs):
                    if hasattr(socket,'default_value'):
                        v=socket.default_value
                        if isinstance(v,bpy.types.ID):v=v.name_full
                        elif hasattr(v,'__len__') and not isinstance(v,str):v=list(v)
                        sockets.append((socket.identifier,v))
                nodes.append([node.name,node.bl_idname,props,sockets])
            return [nodes,[(x.from_node.name,x.from_socket.identifier,x.to_node.name,x.to_socket.identifier) for x in tree.links]]
        value=[mat.name_full,list(mat.diffuse_color),mat.use_backface_culling,tree_value(mat.node_tree) if mat.node_tree else None]
        result=(digest(value),world);self.materials[mat.as_pointer()]=result;return result

class BakeCache:
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.fingerprints=Fingerprints();self.hits=0;self.misses=0
    def key(self,mesh,source,world,actor_id,materials,baker):
        deps=[self.fingerprints.material(m) for m in materials]
        spatial=any(x[1] for x in deps)
        def non_square_image(tree,seen):
            if not tree or tree.as_pointer() in seen:return False
            seen.add(tree.as_pointer())
            for node in tree.nodes:
                image=getattr(node,'image',None)
                if image and image.size[0]!=image.size[1]:return True
                if node.type=='GROUP' and non_square_image(node.node_tree,seen):return True
            return False
        # Square source images produce the same packing with aspect correction
        # disabled. Refresh old atlases whenever non-square sources could have
        # influenced Blender's previous smart-project aspect correction.
        atlas_version=VERSION+('-square-atlas-v2' if any(non_square_image(m.node_tree,set()) for m in materials if m and m.use_nodes) else '')
        from .material_graph import optical_profile
        if any(optical_profile(m,baker.source_engine)['shading_model']=='cloth' for m in materials):
            atlas_version+='-premultiplied-fuzz-v1'
        self.last_components=[atlas_version,bpy.app.version_string,actor_id,geometry_hash(mesh),deps,
                       [list(r) for r in world] if spatial else None,list(source.color),source.pass_index,
                       baker.source_engine,baker.resolution,baker.samples]
        return digest(self.last_components)
    def bake(self,baker,mesh,source,world,actor_id,materials):
        key=self.key(mesh,source,world,actor_id,materials,baker)
        meta=self.directory/(key[:24]+'.json');uvfile=self.directory/(key[:24]+'.npz')
        try:
            data=json.loads(meta.read_text())
            if data['key']!=key or not all(Path(t['file']).is_file() for t in data['textures']):raise ValueError('Missing cached payload')
            with np.load(uvfile) as archive:uv=archive['uv']
            if len(uv)!=len(mesh.loops)*2:raise ValueError('Cached atlas does not match topology')
            clone=mesh.copy();baker.meshes.append(clone)
            layer=clone.uv_layers.new(name=data['uv_name']);layer.data.foreach_set('uv',uv);layer.active_render=True
            for warning in data['warnings']:baker.warn(warning)
            self.hits+=1
            return clone,copy.deepcopy(data['materials']),copy.deepcopy(data['textures'])
        except (OSError,ValueError,KeyError):pass
        self.misses+=1
        warnings=[];original_warn=baker.warn;old_staging=baker.staging
        # Immutable generation-specific filenames, with a short common directory.
        baker.content_key=key;baker.staging=self.directory
        baker.warn=lambda msg:(warnings.append(msg),original_warn(msg))
        try:result=baker.bake_object(mesh,source,world,actor_id,source_materials=materials)
        finally:baker.warn=original_warn;baker.staging=old_staging
        clone,mats,textures=result;uv=next(u for u in clone.uv_layers if u.active_render)
        np.savez(uvfile,uv=array(uv.data,'uv',2))
        data=dict(key=key,dependencies=self.last_components,uv_name=uv.name,materials=mats,textures=textures,warnings=list(dict.fromkeys(warnings)))
        temp=meta.with_suffix('.tmp');temp.write_text(json.dumps(data));temp.replace(meta)
        return result


class ExtractionCache:
    """Reuse evaluated records only for source inputs whose dependencies we model.

    Unknown modifier/animation dependencies deliberately take the full path.
    This also avoids nondeterministic Subsurf UV reevaluation on unchanged input.
    """
    LOCAL_MODIFIERS={'SUBSURF','MIRROR','BEVEL','SOLIDIFY','TRIANGULATE','WEIGHTED_NORMAL','EDGE_SPLIT','DECIMATE','REMESH','WELD','SMOOTH'}
    def __init__(self):self.records={}
    def begin(self,settings,scene):
        self.settings=settings;self.scene=scene;self.fingerprints=Fingerprints();self.geometry={};self.hits=0;self.misses=0;self.seen=set()
    def key(self,obj,world,actor_id):
        self.seen.add(actor_id)
        if obj.type!='MESH' or obj.data.shape_keys or obj.animation_data or obj.data.animation_data or obj.constraints:return None
        modifiers=[]
        for modifier in obj.modifiers:
            if modifier.type not in self.LOCAL_MODIFIERS:return None
            props={}
            for prop in modifier.bl_rna.properties:
                if prop.identifier=='rna_type':continue
                value=getattr(modifier,prop.identifier)
                if prop.type in {'BOOLEAN','INT','FLOAT','STRING','ENUM'}:
                    props[prop.identifier]=list(value) if getattr(prop,'is_array',False) else value
                elif prop.type=='POINTER' and isinstance(value,bpy.types.ID):return None
            modifiers.append(props)
        source=obj.data
        if source.as_pointer() not in self.geometry:self.geometry[source.as_pointer()]=geometry_hash(source)
        mats=[self.fingerprints.material(slot.material) for slot in obj.material_slots]
        weights=[(v.index,[(g.group,g.weight) for g in v.groups]) for v in source.vertices] if obj.vertex_groups else []
        # A camera/scene-dependent shader cannot be cached merely from placement.
        for slot in obj.material_slots:
            if slot.material and (slot.material.animation_data or (slot.material.node_tree and slot.material.node_tree.animation_data)):return None
        return digest([VERSION,self.settings,self.scene.frame_current,self.scene.render.engine,
                       self.scene.unit_settings.scale_length,obj.name,self.geometry[source.as_pointer()],modifiers,
                       mats,[list(row) for row in world] if any(m[1] for m in mats) else None,
                       list(obj.color),obj.pass_index,weights,[g.name for g in obj.vertex_groups],[slot.link for slot in obj.material_slots]])
    def get(self,actor_id,key):
        r=self.records.get(actor_id)
        if key and r and r['key']==key and all(Path(t['file']).is_file() for t in r['textures']) and Path(r['mesh'].get('binary_file','')).is_file():
            self.hits+=1;return r
        self.misses+=1;return None
    def put(self,actor_id,key,mesh,materials,textures,translation,warnings,bake_warnings):
        if key:self.records[actor_id]=copy.deepcopy(dict(key=key,mesh=mesh,materials=materials,textures=textures,translation=translation,warnings=warnings,bake_warnings=bake_warnings))
    def finish(self):
        self.records={k:v for k,v in self.records.items() if k in self.seen}

