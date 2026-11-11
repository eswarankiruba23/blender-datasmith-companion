"""Small typed Blender shader compiler using public Datasmith expressions.

Only the documented subset is translated. Unsupported channels retain a clear
warning and their socket fallback; Cycles baking remains an explicit option.
No external Unreal material-function assets are required.
"""
import math


class UnsupportedMaterial(ValueError):
    pass


def material_output(tree, engine='BLENDER_EEVEE'):
    if not tree:
        return None
    target = 'CYCLES' if engine == 'CYCLES' else 'EEVEE'
    return tree.get_output_node(target) or tree.get_output_node('ALL')


def require_direct_image(image):
    if image.source in {'TILED', 'MOVIE', 'SEQUENCE'}:
        raise UnsupportedMaterial(f'Image {image.name}: {image.source} requires current-frame atlas baking')
    if not image.size[0] or not image.size[1]:
        raise UnsupportedMaterial(f'Image {image.name}: missing image pixels')
    return image.name


def specular_from_ior(ior, level):
    ior = max(1.0, float(ior))
    return 25.0 * float(level) * ((ior-1.0)/(ior+1.0))**2


def unsupported_principled(node):
    # Non-default optical lobes cannot be reproduced by a six-channel atlas.
    defaults = {'Subsurface Weight':0, 'Anisotropic':0,
                'Anisotropic IOR Level':0, 'Thin Film Thickness':0,
                'Specular Tint':(1,1,1,1), 'Coat Tint':(1,1,1,1), 'Coat IOR':1.5}
    for name, default in defaults.items():
        socket = node.inputs.get(name)
        if socket is None: continue
        if name.startswith('Coat ') and not node.inputs['Coat Weight'].is_linked and node.inputs['Coat Weight'].default_value == 0: continue
        value = socket.default_value
        different = any(abs(a-b)>1e-6 for a,b in zip(value,default)) if isinstance(default,tuple) else abs(value-default)>1e-6
        if socket.is_linked or different: yield name
    socket=node.inputs.get('Coat Normal')
    if socket and socket.is_linked: yield 'Coat Normal'


def validate_graph(graph):
    widths=[]
    def width(ref):
        index=ref['node']
        if not 0 <= index < len(widths): raise UnsupportedMaterial('Invalid expression dependency')
        return 1 if ref.get('output',0) else widths[index]
    for node in graph['nodes']:
        op=node['op']; w=[width(r) for r in node.get('inputs',[])]
        if op=='scalar': n=1
        elif op in {'color','texture'}: n=3
        elif op=='uv': n=2
        elif op=='mask':
            if any('RGBA'.index(c)>=w[0] for c in node['channels']): raise UnsupportedMaterial('Component mask reads a missing channel')
            n=len(node['channels'])
        elif op=='append': n=sum(w)
        elif op=='dot': n=1
        else: n=max(w)
        widths.append(n)
    for ref in graph['outputs'].values(): width(ref)
    return graph


class MaterialGraph:
    MAX_NODES = 2048
    MAX_DEPTH = 64

    def __init__(self, material, uv_names, texture, warn, engine='BLENDER_EEVEE'):
        self.material, self.uv_names = material, list(uv_names)
        self.texture, self.warn, self.engine = texture, warn, engine
        self.nodes, self.constants, self.cache, self.busy = [], {}, {}, set()
        self.bake_reasons = []

    def warning(self, message, bake=True):
        if bake:
            self.bake_reasons.append(message)
        suffix = '; use baking for this channel' if bake else '; baking does not reproduce this effect'
        self.warn(f'{self.material.name}: {message}{suffix}')

    def emit(self, op, **values):
        if len(self.nodes) >= self.MAX_NODES:
            raise UnsupportedMaterial('material expression limit exceeded')
        index = len(self.nodes)
        self.nodes.append(dict(op=op, **values))
        return dict(node=index, output=0)

    def scalar(self, value):
        value = float(value)
        if not math.isfinite(value):
            raise UnsupportedMaterial('non-finite scalar')
        ref = self.emit('scalar', value=value)
        self.constants[ref['node']] = value
        return ref

    def color(self, value):
        value = [float(x) for x in value[:3]]
        if len(value) != 3 or not all(math.isfinite(x) for x in value):
            raise UnsupportedMaterial('non-finite or invalid color/vector')
        ref = self.emit('color', value=value)
        self.constants[ref['node']] = value
        return ref

    def constant(self, ref):
        return self.constants.get(ref['node']) if ref.get('output', 0) == 0 else None

    def op(self, name, *inputs, **props):
        return self.emit(name, inputs=list(inputs), **props)

    def clamp(self, ref, low=0.0, high=1.0):
        value = self.constant(ref)
        if isinstance(value, (float, int)):
            return self.scalar(max(low, min(high, value)))
        return self.op('clamp', ref, self.scalar(low), self.scalar(high))

    def mask(self, ref, channels):
        return self.op('mask', ref, channels=channels)

    def vector(self, x, y, z=None):
        xy = self.op('append', x, y)
        return self.op('append', xy, z) if z is not None else xy

    @staticmethod
    def _matching(sockets, socket):
        # Identifiers remain distinct when a group has duplicate socket labels.
        return next((s for s in sockets if s.identifier == socket.identifier), None)

    def source(self, socket, context=(), depth=0):
        """Resolve transparent plumbing without losing group-instance context."""
        if depth > self.MAX_DEPTH:
            raise UnsupportedMaterial('material group depth exceeded')
        if not socket.is_linked:
            return socket, context
        source = socket.links[0].from_socket
        node = source.node
        if node.type == 'REROUTE':
            return self.source(node.inputs[0], context, depth+1)
        if node.mute:
            internal = next((l for l in node.internal_links if l.to_socket == source), None)
            if internal:
                return self.source(internal.from_socket, context, depth+1)
        if node.type == 'GROUP' and node.node_tree:
            output = next((n for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT' and n.is_active_output), None)
            inner = self._matching(output.inputs, source) if output else None
            if inner is None:
                raise UnsupportedMaterial('group output is missing')
            return self.source(inner, context+(node,), depth+1)
        if node.type == 'GROUP_INPUT' and context:
            outer = self._matching(context[-1].inputs, source)
            if outer is None:
                raise UnsupportedMaterial('group input is missing')
            return self.source(outer, context[:-1], depth+1)
        return source, context

    def default(self, socket):
        if socket.type == 'VALUE':
            return self.scalar(socket.default_value)
        if socket.type in {'RGBA', 'VECTOR'}:
            return self.color(socket.default_value)
        raise UnsupportedMaterial(f'unsupported socket type {socket.type}')

    def coerce(self, result, source_type, target_type):
        if source_type == 'VALUE' and target_type in {'RGBA','VECTOR'}:
            value=self.constant(result)
            return self.color((value,value,value)) if isinstance(value,(int,float)) else self.vector(result,result,result)
        if target_type == 'VALUE' and source_type in {'RGBA','VECTOR'}:
            weights=(0.2126,0.7152,0.0722) if source_type=='RGBA' else (1/3,1/3,1/3)
            value=self.constant(result)
            return self.scalar(sum(a*b for a,b in zip(value,weights))) if isinstance(value,list) else self.op('dot',result,self.color(weights))
        return result

    def input(self, socket, context=(), depth=0):
        if depth > self.MAX_DEPTH: raise UnsupportedMaterial('material graph depth exceeded')
        if not socket.is_linked: return self.default(socket)
        source=socket.links[0].from_socket; node=source.node
        if node.type == 'REROUTE':
            result=self.input(node.inputs[0],context,depth+1)
        elif node.mute:
            internal=next((l for l in node.internal_links if l.to_socket==source),None)
            if internal is None: raise UnsupportedMaterial('Muted node has no passthrough')
            result=self.coerce(self.input(internal.from_socket,context,depth+1),internal.from_socket.type,source.type)
        elif node.type=='GROUP' and node.node_tree:
            output=next((n for n in node.node_tree.nodes if n.type=='GROUP_OUTPUT' and n.is_active_output),None)
            inner=self._matching(output.inputs,source) if output else None
            if inner is None: raise UnsupportedMaterial('group output is missing')
            result=self.coerce(self.input(inner,context+(node,),depth+1),inner.type,source.type)
        elif node.type=='GROUP_INPUT' and context:
            outer=self._matching(context[-1].inputs,source)
            if outer is None: raise UnsupportedMaterial('group input is missing')
            result=self.coerce(self.input(outer,context[:-1],depth+1),outer.type,source.type)
        else: result=self.output(source,context,depth+1)
        return self.coerce(result,source.type,socket.type)

    def output(self, socket, context=(), depth=0):
        if depth > self.MAX_DEPTH:
            raise UnsupportedMaterial('material graph depth exceeded')
        node = socket.node
        key = (node.as_pointer(), socket.identifier, tuple(n.as_pointer() for n in context))
        if key in self.cache:
            return self.cache[key]
        if key in self.busy:
            raise UnsupportedMaterial('recursive material graph')
        self.busy.add(key)
        try:
            result = self._output(socket, context, depth)
            self.cache[key] = result
            return result
        finally:
            self.busy.discard(key)

    def uv(self, name=''):
        if name and name not in self.uv_names:
            raise UnsupportedMaterial(f'UV map {name!r} was not exported')
        if not self.uv_names:
            raise UnsupportedMaterial('image material has no exported UV map')
        return self.emit('uv', index=self.uv_names.index(name) if name else 0)

    def blender_uv(self, uv):
        # Mesh payloads use Unreal V=1-BlenderV. Graph coordinate math is done
        # in Blender's original coordinate system then reflected for sampling.
        return self.vector(self.mask(uv, 'R'), self.op('subtract', self.scalar(1), self.mask(uv, 'G')), self.scalar(0))

    def texture_uv(self, node, context, depth):
        vector = node.inputs['Vector']
        if not vector.is_linked:
            return self.uv()
        coords = self.input(vector, context, depth+1)
        return self.vector(self.mask(coords, 'R'), self.op('subtract', self.scalar(1), self.mask(coords, 'G')))

    def image(self, node, socket, context, depth, normal=False):
        if node.image is None:
            raise UnsupportedMaterial(f'{node.name} has no image')
        require_direct_image(node.image)
        if node.projection != 'FLAT':
            raise UnsupportedMaterial(f'image projection {node.projection} is unsupported')
        if node.extension != 'REPEAT':
            raise UnsupportedMaterial(f'image extension {node.extension} is unsupported')
        if node.interpolation not in {'Linear', 'Smart'}:
            raise UnsupportedMaterial(f'image interpolation {node.interpolation} is unsupported')
        uv = self.texture_uv(node, context, depth)
        ref = self.emit('texture', texture=self.texture(node.image, normal), uv=uv, normal=normal)
        if socket.name == 'Alpha':
            ref = dict(ref, output=4)
        return ref

    def _output(self, socket, context, depth):
        node = socket.node
        inp = lambda s: self.input(s, context, depth+1)
        if node.type == 'VALUE':
            return self.scalar(socket.default_value)
        if node.type == 'RGB':
            return self.color(socket.default_value)
        if node.type == 'TEX_IMAGE':
            return self.image(node, socket, context, depth)
        if node.type == 'UVMAP':
            return self.blender_uv(self.uv(node.uv_map))
        if node.type == 'TEX_COORD':
            if socket.name != 'UV':
                raise UnsupportedMaterial(f'Texture Coordinate {socket.name} is unsupported')
            return self.blender_uv(self.uv())
        if node.type == 'MAPPING':
            return self.mapping(node, context, depth)
        if node.type == 'MATH':
            a, b = inp(node.inputs[0]), inp(node.inputs[1])
            op = {'ADD':'add', 'SUBTRACT':'subtract', 'MULTIPLY':'multiply', 'DIVIDE':'divide'}.get(node.operation)
            if op is None:
                raise UnsupportedMaterial(f'Math {node.operation} is unsupported')
            av, bv = self.constant(a), self.constant(b)
            if op == 'divide' and not isinstance(bv, (float, int)):
                raise UnsupportedMaterial('Math Divide requires a constant denominator')
            if op == 'divide' and bv == 0:
                result = self.scalar(0)  # Blender safe division.
            elif isinstance(av, (float, int)) and isinstance(bv, (float, int)):
                value = {'add':lambda:av+bv, 'subtract':lambda:av-bv, 'multiply':lambda:av*bv, 'divide':lambda:av/bv}[op]()
                result = self.scalar(value)
            else:
                result = self.op(op, a, b)
            return self.clamp(result) if node.use_clamp else result
        if node.type == 'MIX_RGB':
            factor, a, b = [inp(s) for s in node.inputs[:3]]
            if node.blend_type == 'MULTIPLY':
                b = self.op('multiply', a, b)
            elif node.blend_type != 'MIX':
                raise UnsupportedMaterial(f'Mix Color {node.blend_type} is unsupported')
            result = self.op('lerp', a, b, self.clamp(factor))
            return self.clamp(result) if node.use_clamp else result
        if node.type == 'MIX':
            if node.data_type == 'FLOAT':
                factor, a, b = inp(node.inputs[0]), inp(node.inputs[2]), inp(node.inputs[3])
            elif node.data_type == 'RGBA':
                factor, a, b = inp(node.inputs[0]), inp(node.inputs[6]), inp(node.inputs[7])
                if node.blend_type != 'MIX':
                    raise UnsupportedMaterial(f'Mix Color {node.blend_type} is unsupported')
            else:
                raise UnsupportedMaterial(f'Mix data type {node.data_type} is unsupported')
            if node.clamp_factor:
                factor = self.clamp(factor)
            result = self.op('lerp', a, b, factor)
            return self.clamp(result) if getattr(node, 'clamp_result', False) else result
        if node.type == 'VALTORGB':
            return self.ramp(node, socket, context, depth)
        if node.type in {'SEPRGB', 'SEPXYZ', 'SEPARATE_COLOR'}:
            if node.type == 'SEPARATE_COLOR' and node.mode != 'RGB':
                raise UnsupportedMaterial(f'Separate Color mode {node.mode} is unsupported')
            index = list(node.outputs).index(socket)
            return self.mask(inp(node.inputs[0]), 'RGB'[index])
        if node.type in {'COMBRGB', 'COMBXYZ', 'COMBINE_COLOR'}:
            if node.type == 'COMBINE_COLOR' and node.mode != 'RGB':
                raise UnsupportedMaterial(f'Combine Color mode {node.mode} is unsupported')
            return self.vector(*(inp(s) for s in node.inputs[:3]))
        if node.type == 'RGBTOBW':
            return self.op('dot', inp(node.inputs[0]), self.color((0.2126, 0.7152, 0.0722)))
        raise UnsupportedMaterial(f'{node.bl_idname} is unsupported')

    def mapping(self, node, context, depth):
        if node.vector_type != 'POINT':
            raise UnsupportedMaterial(f'Mapping type {node.vector_type} is unsupported')
        values = []
        for name in ('Location', 'Rotation', 'Scale'):
            ref = self.input(node.inputs[name], context, depth+1)
            value = self.constant(ref)
            if not isinstance(value, list):
                raise UnsupportedMaterial('Mapping transform inputs must be constant vectors')
            values.append(value)
        location, rotation, scale = values
        if abs(rotation[0]) > 1e-7 or abs(rotation[1]) > 1e-7:
            raise UnsupportedMaterial('Mapping X/Y rotation is unsupported')
        source = self.input(node.inputs['Vector'], context, depth+1)
        x = self.op('multiply', self.mask(source, 'R'), self.scalar(scale[0]))
        y = self.op('multiply', self.mask(source, 'G'), self.scalar(scale[1]))
        cs, sn = math.cos(rotation[2]), math.sin(rotation[2])
        rx = self.op('subtract', self.op('multiply', x, self.scalar(cs)), self.op('multiply', y, self.scalar(sn)))
        ry = self.op('add', self.op('multiply', x, self.scalar(sn)), self.op('multiply', y, self.scalar(cs)))
        return self.vector(self.op('add', rx, self.scalar(location[0])), self.op('add', ry, self.scalar(location[1])), self.op('add',self.op('multiply',self.mask(source,'B'),self.scalar(scale[2])),self.scalar(location[2])))

    def ramp(self, node, socket, context, depth):
        ramp = node.color_ramp
        if ramp.color_mode != 'RGB' or ramp.interpolation != 'LINEAR':
            raise UnsupportedMaterial('Color Ramp currently requires RGB/Linear interpolation')
        elements = list(ramp.elements)
        if len(elements) > 16:
            raise UnsupportedMaterial('Color Ramp exceeds 16 stops')
        if any(b.position <= a.position for a,b in zip(elements, elements[1:])):
            raise UnsupportedMaterial('Color Ramp duplicate stop positions are unsupported')
        factor = self.input(node.inputs['Fac'], context, depth+1)
        color = lambda e: self.scalar(e.color[3]) if socket.name == 'Alpha' else self.color(e.color)
        result = color(elements[0])
        # A sum of clamped segment deltas represents an arbitrary piecewise
        # linear ramp, including the constant tails outside its first/last stop.
        for left, right in zip(elements, elements[1:]):
            t = self.clamp(self.op('divide', self.op('subtract', factor, self.scalar(left.position)), self.scalar(right.position-left.position)))
            delta = self.op('subtract', color(right), color(left))
            result = self.op('add', result, self.op('multiply', delta, t))
        return result

    def normal(self, socket, context):
        source, context = self.source(socket, context)
        node = source.node
        if not source.is_output or node.type != 'NORMAL_MAP' or node.space != 'TANGENT':
            raise UnsupportedMaterial('normal input requires a tangent Normal Map node')
        if node.uv_map and (node.uv_map not in self.uv_names or self.uv_names.index(node.uv_map) != 0):
            raise UnsupportedMaterial(f'normal tangent UV {node.uv_map!r} differs from exported tangent UV0')
        color, color_context = self.source(node.inputs['Color'], context)
        if not color.is_output or color.node.type != 'TEX_IMAGE' or color.name != 'Color':
            raise UnsupportedMaterial('Normal Map color requires an image through groups/reroutes')
        decoded = self.image(color.node, color, color_context, 0, normal=True)
        strength = self.input(node.inputs['Strength'], context)
        # Blender's tangent-space strength scales XY, blends Z from flat using
        # saturated strength, then normalizes. UE normal textures are decoded.
        x = self.op('multiply', self.mask(decoded, 'R'), strength)
        y = self.op('multiply', self.mask(decoded, 'G'), strength)
        z = self.op('lerp', self.scalar(1), self.mask(decoded, 'B'), self.clamp(strength))
        return self.op('normalize', self.vector(x,y,z))

    def compile(self, data):
        output = material_output(self.material.node_tree, self.engine)
        if not output:
            raise UnsupportedMaterial('no material output for the active render engine')
        if output.inputs['Volume'].is_linked:
            self.warning('volume is not translated', bake=False)
        if output.inputs['Displacement'].is_linked:
            self.warning('shader displacement is not translated; use evaluated geometry', bake=False)
        surface, context = self.source(output.inputs['Surface'])
        node = surface.node
        data.update(optical_profile(self.material, self.engine))
        for message in optical_limits(data):self.warn(message)
        if surface.is_output and node.type in {'BSDF_GLASS','BSDF_REFRACTION','BSDF_SHEEN'}:
            outputs = dict(base_color=self.input(node.inputs['Color'],context),
                           roughness=self.input(node.inputs['Roughness'],context),metallic=self.scalar(0))
            if node.type=='BSDF_SHEEN':
                outputs.update(cloth=self.scalar(1),fuzz_color=outputs['base_color'])
            else:
                ior=self.input(node.inputs['IOR'],context)
                value=self.constant(ior)
                if value is None: raise UnsupportedMaterial('Varying glass IOR requires baking approximation')
                outputs.update(transmission=self.scalar(1),specular=self.scalar(min(1,specular_from_ior(value,.5))))
            if node.inputs['Normal'].is_linked: outputs['normal']=self.normal(node.inputs['Normal'],context)
            data['graph']=validate_graph(dict(version=1,nodes=self.nodes,outputs=outputs))
            data['bake_reasons']=self.bake_reasons
            return data
        # Blender implicitly converts colors/values connected to Surface into
        # emission. Common picture assets use this without an explicit BSDF.
        if surface.type in {'VALUE','RGBA','VECTOR'} or (surface.is_output and node.type=='EMISSION'):
            if node.type=='EMISSION':
                emission=self.op('multiply',self.input(node.inputs['Color'],context),self.input(node.inputs['Strength'],context))
            else:
                emission=self.input(output.inputs['Surface'])
                if surface.type=='VALUE':emission=self.coerce(emission,'VALUE','RGBA')
            data.update(base_color=[0,0,0,1],metallic=0,roughness=1,specular=0,opacity=1,coat=0,emission=[0,0,0,1],alpha_texture=False)
            data['graph']=validate_graph(dict(version=1,nodes=self.nodes,outputs=dict(base_color=self.color((0,0,0)),metallic=self.scalar(0),roughness=self.scalar(1),specular=self.scalar(0),emission=emission)))
            data['bake_reasons']=self.bake_reasons
            return data
        if not surface.is_output or node.type != 'BSDF_PRINCIPLED':
            raise UnsupportedMaterial('surface must resolve to one Principled BSDF through groups/reroutes')
        outputs = {}
        channels = {'base_color':'Base Color','metallic':'Metallic','roughness':'Roughness','specular':'Specular IOR Level',
                    'opacity':'Alpha','coat':'Coat Weight','coat_roughness':'Coat Roughness','emission':'Emission Color'}
        coat_socket=node.inputs.get('Coat Weight')
        if data.get('shading_model') in {'glass','cloth'} and coat_socket and (coat_socket.is_linked or coat_socket.default_value>0):
            self.warn('Glass/cloth shading does not retain a separate Principled coat layer')
        if data.get('shading_model')=='glass': channels['transmission']='Transmission Weight'
        if data.get('shading_model')=='cloth':
            channels.pop('coat');channels.pop('coat_roughness')
            channels.update(cloth='Sheen Weight',fuzz_color='Sheen Tint')
        for kind, name in channels.items():
            if kind == 'coat_roughness' and 'coat' not in outputs:
                continue
            socket = node.inputs.get(name)
            if socket is None:
                continue
            value = socket.default_value
            data[kind] = list(value) if kind in {'base_color', 'emission','fuzz_color'} else float(value)
            try:
                ref = self.input(socket, context)
                if kind in {'transmission','cloth'}:ref=self.clamp(ref)
                if kind == 'specular':
                    ior=self.input(node.inputs['IOR'],context)
                    iv,sv=self.constant(ior),self.constant(ref)
                    if isinstance(iv,(int,float)) and isinstance(sv,(int,float)):
                        value=specular_from_ior(iv,sv)
                        if value>1: self.warning('IOR reflectance exceeds standard Unreal Specular range; clamped',bake=False)
                        data[kind]=min(1,max(0,value));ref=self.scalar(data[kind])
                    else:
                        self.warning('Varying IOR uses standard Unreal reflectance, clamped to its range',bake=False)
                        ior=self.clamp(ior,1,1000)
                        ratio=self.op('divide',self.op('subtract',ior,self.scalar(1)),self.op('add',ior,self.scalar(1)))
                        ref=self.clamp(self.op('multiply',self.op('multiply',ratio,ratio),self.op('multiply',ref,self.scalar(25))))
                if kind == 'coat':
                    coat = self.constant(ref)
                    if isinstance(coat,(float,int)) and coat <= 0:
                        continue
                if kind == 'emission':
                    strength_socket = node.inputs.get('Emission Strength')
                    strength = self.input(strength_socket, context) if strength_socket else self.scalar(1)
                    ref = self.op('multiply', ref, strength)
                    if strength_socket and not strength_socket.is_linked:
                        data[kind][:3] = [v*strength_socket.default_value for v in data[kind][:3]]
                if kind == 'opacity':
                    alpha = self.constant(ref)
                    if isinstance(alpha,(float,int)) and alpha >= .999:
                        data['opacity'] = 1.0
                        continue
                    data['alpha_texture'] = True
                if self.nodes[ref['node']]['op'] in {'scalar', 'color'}:
                    # Each semantic output gets a separate named constant even
                    # when a shared Value/RGB node feeds several destinations.
                    value = self.constant(ref)
                    ref = self.color(value) if isinstance(value,list) else self.scalar(value)
                    self.nodes[ref['node']]['name'] = kind
                outputs[kind] = ref
            except UnsupportedMaterial as exc:
                self.warning(f'{name}: {exc}; using socket default')
                fallback = data[kind]
                if kind != 'opacity' or float(fallback) < .999:
                    ref = self.color(fallback) if isinstance(fallback,list) else self.scalar(fallback)
                    self.nodes[ref['node']]['name'] = kind
                    outputs[kind] = ref
        normal = node.inputs.get('Normal')
        if normal and normal.is_linked:
            try:
                outputs['normal'] = self.normal(normal, context)
            except UnsupportedMaterial as exc:
                self.warning(f'Normal: {exc}; keeping geometric normal')
        for name in unsupported_principled(node):
            self.warning(f'{name} is not translated', bake=False)
        data['graph'] = validate_graph(dict(version=1, nodes=self.nodes, outputs=outputs))
        data['bake_reasons'] = self.bake_reasons
        return data


def optical_limits(profile):
    if profile.get('shading_model')=='glass':
        yield 'Glass uses Unreal Thin Translucent: tint and transmission are retained, but solid-volume refraction, caustics and absorption depth are approximated'
        if profile.get('cloth',0)>0:yield 'Combined transmission and sheen uses glass shading; a simultaneous cloth lobe is not supported'
    if profile.get('shading_model')=='cloth':
        yield 'Sheen uses Unreal Cloth: amount and fuzz color are retained; separate sheen roughness and the exact Blender BRDF are approximated'
        yield 'Cloth opacity uses masked coverage; partially transparent fabric needs a separate translucency approximation'


def optical_profile(mat, engine='BLENDER_EEVEE'):
    """Classify reachable closures, resolving group-instance switches, never names.

    Unknown varying mixtures retain weighted baked masks. A disabled glass branch
    in a reusable plastic group must not turn the plastic into glass.
    """
    result=dict(shading_model='default',transmission=0.0,cloth=0.0,fuzz_color=[1,1,1,1])
    if not mat or not mat.use_nodes:return result
    graph=MaterialGraph(mat,[],lambda *a,**k: '',lambda *a:None,engine)
    def value(socket,ctx,depth=0):
        if depth>64:return None
        s,c=graph.source(socket,ctx);n=s.node
        if not s.is_output:return float(s.default_value) if s.type=='VALUE' else None
        if n.type=='VALUE':return float(s.default_value)
        if n.type=='MATH':
            v=[value(x,c,depth+1) for x in n.inputs]
            if n.operation=='MULTIPLY' and (v[0]==0 or v[1]==0):return 0.0
            if v[0] is None or v[1] is None:return None
            ops={'ADD':lambda:v[0]+v[1],'SUBTRACT':lambda:v[0]-v[1],
                 'MULTIPLY':lambda:v[0]*v[1],'DIVIDE':lambda:v[0]/v[1] if v[1] else 0,
                 'LESS_THAN':lambda:float(v[0]<v[1]),'GREATER_THAN':lambda:float(v[0]>v[1]),
                 'MINIMUM':lambda:min(v[:2]),'MAXIMUM':lambda:max(v[:2])}
            if n.operation in ops:
                z=ops[n.operation]();return max(0,min(1,z)) if n.use_clamp else z
        return None
    def walk(socket,ctx=(),depth=0):
        if depth>64:return
        s,c=graph.source(socket,ctx);n=s.node
        if not s.is_output:return
        if n.type in {'MIX_SHADER','ADD_SHADER'}:
            if n.type=='MIX_SHADER':
                f=value(n.inputs[0],c)
                choices=[n.inputs[1]] if f is not None and f<=0 else [n.inputs[2]] if f is not None and f>=1 else n.inputs[1:]
            else:choices=n.inputs[:]
            for x in choices:walk(x,c,depth+1)
        elif n.type in {'BSDF_GLASS','BSDF_REFRACTION'}:result['transmission']=1.0
        elif n.type=='BSDF_SHEEN':result['cloth']=1.0
        elif n.type=='BSDF_PRINCIPLED':
            for key,name in [('transmission','Transmission Weight'),('cloth','Sheen Weight')]:
                socket=n.inputs.get(name)
                z=value(socket,c) if socket else 0
                result[key]=max(result[key],1.0 if z is None else max(0,min(1,z)))
    output=material_output(mat.node_tree,engine)
    try:
        if output:walk(output.inputs['Surface'])
    except UnsupportedMaterial: return result
    result['shading_model']='glass' if result['transmission']>0 else 'cloth' if result['cloth']>0 else 'default'
    return result


def translate_material(mat, data, uv_names, texture, warn, engine='BLENDER_EEVEE'):
    try:
        MaterialGraph(mat, uv_names, texture, warn, engine).compile(data)
    except UnsupportedMaterial as exc:
        data['base_color'] = list(mat.diffuse_color)
        data['bake_reasons'] = [str(exc)] if mat.node_tree else []
        warn(f'{mat.name}: {exc}; using diffuse fallback. Enable Bake complex materials for broader shader support')
    return data


def assess_material(mat, uv_names, engine='BLENDER_EEVEE'):
    """Preflight without exporting images or mutating source materials."""
    if mat is None or not mat.node_tree:
        return dict(bake=False, reasons=[], warnings=[])
    warnings = []
    data = dict(base_color=list(mat.diffuse_color), opacity=1, alpha_texture=False)
    translate_material(mat, data, uv_names, lambda image, normal=False: image.name, warnings.append, engine)
    reasons = data.get('bake_reasons', [])
    if reasons:
        # Do not turn a volume-only or unsupported closure into a failed scene export.
        from .baking import shader_leaves
        output = material_output(mat.node_tree, engine)
        try:
            if output is None:
                raise ValueError('no active surface output')
            shader_leaves(mat.node_tree, output.inputs['Surface'])
        except ValueError as exc:
            return dict(bake=False, reasons=reasons, warnings=warnings, unavailable=str(exc))
    return dict(bake=bool(reasons), reasons=reasons, warnings=warnings)
