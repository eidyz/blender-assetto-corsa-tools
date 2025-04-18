# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright (C) 2014  Thomas Hagnhofer


import os
import re
import bmesh
from mathutils import Matrix, Vector
from .exporter_utils import (
    convert_matrix,
    convert_vector3,
    get_active_material_texture_slot,
)
from .kn5_writer import KN5Writer
from ..utils.constants import ASSETTO_CORSA_OBJECTS


NODES = "nodes"

NODE_CLASS = {
    "Node" : 1,
    "Mesh" : 2,
    "SkinnedMesh" : 3,
}

NODE_SETTINGS = (
    "lodIn",
    "lodOut",
    "layer",
    "castShadows",
    "visible",
    "transparent",
    "renderable",
)


class UvVertex:
    def __init__(self, co, normal, uv, tangent):
        self.co = co
        self.normal = normal
        self.uv = uv
        self.tangent = tangent
    
    def __eq__(self, other):
        if not isinstance(other, UvVertex):
            return False
        return (self.co == other.co and
                self.normal == other.normal and
                self.uv == other.uv)
    
    def __hash__(self):
        return hash((self.co[0], self.co[1], self.co[2],
                     self.normal[0], self.normal[1], self.normal[2],
                     self.uv[0], self.uv[1]))


class Mesh:
    def __init__(self, material_id, vertices, indices):
        self.material_id = material_id
        self.vertices = vertices
        self.indices = indices


class NodeProperties:
    def __init__(self, obj):
        self.lodIn = 0
        self.lodOut = 1000
        self.layer = 0
        self.castShadows = True
        self.visible = True
        self.transparent = False
        self.renderable = True
        
        # Use custom properties from the object if they exist
        if obj and hasattr(obj, "ac_properties"):
            props = obj.ac_properties
            for prop_name in NODE_SETTINGS:
                if hasattr(props, prop_name):
                    setattr(self, prop_name, getattr(props, prop_name))


class NodeSettings:
    def __init__(self, settings, node_key):
        self.node_regexp = re.compile(node_key)
        self.settings = {}
        for setting_name in NODE_SETTINGS:
            if setting_name in settings[NODES][node_key]:
                self.settings[setting_name] = settings[NODES][node_key][setting_name]
    
    def apply_settings_to_node(self, node_properties):
        for setting_name, setting_value in self.settings.items():
            setattr(node_properties, setting_name, setting_value)


class NodeWriter(KN5Writer):
    def __init__(self, file, context, settings, warnings, material_writer):
        super().__init__(file)

        self.context = context
        self.settings = settings
        self.warnings = warnings
        self.material_writer = material_writer
        self.scene = self.context.scene
        self.node_settings = []
        self.ac_objects = []
        self._init_assetto_corsa_objects()
        self._init_node_settings()

    def _init_node_settings(self):
        self.node_settings = []
        if NODES in self.settings:
            for node_key in self.settings[NODES]:
                self.node_settings.append(NodeSettings(self.settings, node_key))

    def _init_assetto_corsa_objects(self):
        for obj_name in ASSETTO_CORSA_OBJECTS:
            self.ac_objects.append(re.compile(f"^{obj_name}$"))

    def _is_ac_object(self, name):
        for regex in self.ac_objects:
            if regex.match(name):
                return True
        return False

    def write(self):
        self._write_base_node(None, "BlenderFile")
        # Only process objects that are in the active view layer and not hidden
        view_layer = self.context.view_layer
        for obj in sorted(self.context.blend_data.objects, key=lambda k: len(k.children)):
            # Skip objects that are in excluded collections
            if not obj.parent and obj.visible_get(view_layer=view_layer):
                self._write_object(obj)

    def _write_object(self, obj):
        view_layer = self.context.view_layer
        if not obj.name.startswith("__") and obj.visible_get(view_layer=view_layer):
            if obj.type == "MESH":
                if obj.children:
                    raise Exception(f"A mesh cannot contain children ('{obj.name}')")
                self._write_mesh_node(obj)
            else:
                self._write_base_node(obj, obj.name)
            for child in obj.children:
                if child.visible_get(view_layer=view_layer):
                    self._write_object(child)

    def _any_child_is_mesh(self, obj):
        for child in obj.children:
            if child.type in ["MESH", "CURVE"] or self._any_child_is_mesh(child):
                return True
        return False

    def _write_base_node(self, obj, node_name):
        node_data = {}
        matrix = None
        num_children = 0
        view_layer = self.context.view_layer
        
        if not obj:
            matrix = Matrix()
            for obj in self.context.blend_data.objects:
                if not obj.parent and not obj.name.startswith("__") and obj.visible_get(view_layer=view_layer):
                    num_children += 1
        else:
            if not self._is_ac_object(obj.name) and not self._any_child_is_mesh(obj):
                msg = f"Unknown logical object '{obj.name}' might prevent other objects from loading.{os.linesep}"
                msg += "\tRename it to '__{obj.name}' if you do not want to export it."
                self.warnings.append(msg)
            matrix = convert_matrix(obj.matrix_local)
            for child in obj.children:
                if not child.name.startswith("__") and child.visible_get(view_layer=view_layer):
                    num_children += 1

        node_data["name"] = node_name
        node_data["childCount"] = num_children
        node_data["active"] = True
        node_data["transform"] = matrix
        self._write_base_node_data(node_data)

    def _write_base_node_data(self, node_data):
        self._write_node_class("Node")
        self.write_string(node_data["name"])
        self.write_uint(node_data["childCount"])
        self.write_bool(node_data["active"])
        self.write_matrix(node_data["transform"])

    def _write_mesh_node(self, obj):
        divided_meshes = self._split_object_by_materials(obj)
        divided_meshes = self._split_meshes_for_vertex_limit(divided_meshes)
        if obj.parent or len(divided_meshes) > 1:
            node_data = {}
            node_data["name"] = obj.name
            node_data["childCount"] = len(divided_meshes)
            node_data["active"] = True
            transform_matrix = Matrix()
            if obj.parent:
                transform_matrix = convert_matrix(obj.parent.matrix_world.inverted())
            node_data["transform"] = transform_matrix
            self._write_base_node_data(node_data)
        node_properties = NodeProperties(obj)
        for node_setting in self.node_settings:
            node_setting.apply_settings_to_node(node_properties)
        for mesh in divided_meshes:
            self._write_mesh(obj, mesh, node_properties)

    def _write_node_class(self, node_class):
        self.write_uint(NODE_CLASS[node_class])

    def _write_mesh(self, obj, mesh, node_properties):
        self._write_node_class("Mesh")
        self.write_string(obj.name)
        self.write_uint(0) # Child count, none allowed
        is_active = True
        self.write_bool(is_active)
        self.write_bool(node_properties.castShadows)
        self.write_bool(node_properties.visible)
        self.write_bool(node_properties.transparent)
        if len(mesh.vertices) > 2**16:
            raise Exception(f"Only {2**16} vertices per mesh allowed. ('{obj.name}')")
        self.write_uint(len(mesh.vertices))
        for vertex in mesh.vertices:
            self.write_vector3(vertex.co)
            self.write_vector3(vertex.normal)
            self.write_vector2(vertex.uv)
            self.write_vector3(vertex.tangent)
        self.write_uint(len(mesh.indices))
        for i in mesh.indices:
            self.write_ushort(i)
        if mesh.material_id is None:
            self.warnings.append(f"No material to mesh '{obj.name}' assigned")
            self.write_uint(0)
        else:
            self.write_uint(mesh.material_id)
        self.write_uint(node_properties.layer) #Layer
        self.write_float(node_properties.lodIn) #LOD In
        self.write_float(node_properties.lodOut) #LOD Out
        self._write_bounding_sphere(mesh.vertices)
        self.write_bool(node_properties.renderable) #isRenderable

    def _write_bounding_sphere(self, vertices):
        max_x = -999999999
        max_y = -999999999
        max_z = -999999999
        min_x = 999999999
        min_y = 999999999
        min_z = 999999999
        for vertex in vertices:
            co = vertex.co
            if co[0] > max_x:
                max_x = co[0]
            if co[0] < min_x:
                min_x = co[0]
            if co[1] > max_y:
                max_y = co[1]
            if co[1] < min_y:
                min_y = co[1]
            if co[2] > max_z:
                max_z = co[2]
            if co[2] < min_z:
                min_z = co[2]

        sphere_center = [
            min_x + (max_x - min_x) / 2,
            min_y + (max_y - min_y) / 2,
            min_z + (max_z - min_z) / 2
        ]
        sphere_radius = max((max_x - min_x) / 2, (max_y - min_y) / 2, (max_z - min_z) / 2) * 2
        self.write_vector3(sphere_center)
        self.write_float(sphere_radius)

    def _split_object_by_materials(self, obj):
        meshes = []
        # Get evaluated object with modifiers applied
        depsgraph = self.context.evaluated_depsgraph_get()
        obj_eval = obj.evaluated_get(depsgraph)
        # Create a mesh from the evaluated object (with modifiers applied)
        mesh_copy = obj_eval.to_mesh()

        bm = bmesh.new()
        bm.from_mesh(mesh_copy)
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.to_mesh(mesh_copy)
        bm.free()

        try:
            mesh_copy.calc_loop_triangles()
            mesh_copy.calc_tangents()
            mesh_vertices = mesh_copy.vertices[:]
            mesh_loops = mesh_copy.loops[:]
            mesh_triangles = mesh_copy.loop_triangles[:]
            uv_layer = mesh_copy.uv_layers.active
            matrix = obj.matrix_world

            if not mesh_copy.materials:
                raise Exception(f"Object '{obj.name}' has no material assigned")

            used_materials = set([triangle.material_index for triangle in mesh_triangles])
            for material_index in used_materials:
                if not mesh_copy.materials[material_index]:
                    raise Exception(f"Material slot {material_index} for object '{obj.name}' has no material assigned")
                material_name = mesh_copy.materials[material_index].name
                if material_name.startswith("__"):
                    raise Exception(f"Material '{material_name}' is ignored but is used by object '{obj.name}'")

                vertices = {}
                indices = []
                for triangle in mesh_triangles:
                    if material_index != triangle.material_index:
                        continue
                    vertex_index_for_face = 0
                    face_indices = []
                    for loop_index in triangle.loops:
                        loop = mesh_loops[loop_index]
                        local_position = matrix @ mesh_vertices[loop.vertex_index].co
                        converted_position = convert_vector3(local_position)
                        converted_normal = convert_vector3(loop.normal)
                        uv = (0, 0)
                        if uv_layer:
                            uv = uv_layer.data[loop_index].uv
                            uv = (uv[0], -uv[1])
                        else:
                            uv = self._calculate_uvs(obj, mesh_copy, material_index, local_position)
                        tangent = loop.tangent
                        vertex = UvVertex(converted_position, converted_normal, uv, tangent)
                        if vertex not in vertices:
                            new_index = len(vertices)
                            vertices[vertex] = new_index
                        face_indices.append(vertices[vertex])
                        vertex_index_for_face += 1
                    indices.extend((face_indices[1], face_indices[2], face_indices[0]))
                    if len(face_indices) == 4:
                        indices.extend((face_indices[2], face_indices[3], face_indices[0]))
                vertices = [v for v, index in sorted(vertices.items(), key=lambda k: k[1])]
                material_id = self.material_writer.material_positions[material_name]
                meshes.append(Mesh(material_id, vertices, indices))
        finally:
            obj.to_mesh_clear()
        return meshes

    def _split_meshes_for_vertex_limit(self, divided_meshes):
        new_meshes = []
        limit = 2**16
        for mesh in divided_meshes:
            if len(mesh.vertices) > limit:
                start_index = 0
                while start_index < len(mesh.indices):
                    vertex_index_mapping = {}
                    new_indices = []
                    for i in range(start_index, len(mesh.indices), 3):
                        start_index += 3
                        face = mesh.indices[i:i+3]
                        for face_index in face:
                            if not face_index in vertex_index_mapping:
                                new_index = len(vertex_index_mapping)
                                vertex_index_mapping[face_index] = new_index
                            new_indices.append(vertex_index_mapping[face_index])
                        if len(vertex_index_mapping) >= limit-3:
                            break
                    verts = [mesh.vertices[v] for v, index in sorted(vertex_index_mapping.items(), key=lambda k: k[1])]
                    new_meshes.append(Mesh(mesh.material_id, verts, new_indices))
            else:
                new_meshes.append(mesh)
        return new_meshes

    def _calculate_uvs(self, obj, mesh, material_id, co):
        size = obj.dimensions
        x = co[0] / size[0]
        y = co[1] / size[1]
        mat = mesh.materials[material_id]
        texture_node = get_active_material_texture_slot(mat)
        if texture_node:
            x *= texture_node.texture_mapping.scale[0]
            y *= texture_node.texture_mapping.scale[1]
            x += texture_node.texture_mapping.translation[0]
            y += texture_node.texture_mapping.translation[1]
        return (x, y)
