'''
Copyright (C) 2013 CG Cookie
http://cgcookie.com
hello@cgcookie.com

Created by Patrick Moore

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

bl_info = {
    "name": "Contour Retopology Tool",
    "description": "A tool to retopologize forms quickly with contour strokes.",
    "author": "Jonathan Williamson, Patrick Moore",
    "version": (1, 2, 2),
    "blender": (2, 7, 2),
    "location": "View 3D > Tool Shelf",
    "warning": '',  # Used for warning icon and text in addons panel
    "wiki_url": "http://cgcookie.com/blender/docs/contour-retopology/",
    "tracker_url": "https://github.com/CGCookie/retopology/issues?labels=Bug&milestone=1&state=open",
    "category": "3D View"}

# Add the current __file__ path to the search path
import sys
import os
sys.path.append(os.path.dirname(__file__))

import copy
import math
import time
from mathutils import Vector
from mathutils.geometry import intersect_line_plane, intersect_point_line

import blf
import bmesh
import bpy
from bpy_extras.view3d_utils import location_3d_to_region_2d, region_2d_to_vector_3d, region_2d_to_location_3d
from bpy.types import Operator, AddonPreferences
from bpy.props import EnumProperty, StringProperty, BoolProperty, IntProperty, FloatVectorProperty, FloatProperty

import contour_utilities
import general_utilities
from contour_classes import ContourCutLine, ExistingVertList, CutLineManipulatorWidget, ContourCutSeries, ContourStatePreserver

from lib import common_drawing

# Create a class that contains all location information for addons
AL = general_utilities.AddonLocator()

# A place to store stokes for later
global contour_cache
contour_cache = {}
contour_undo_cache = []

# Store any temporary triangulated objects
# Store the bmesh to prevent recalcing bmesh each time :-)
global contour_mesh_cache
contour_mesh_cache = {}


def object_validation(ob):
    me = ob.data

    # Get object data to act as a hash
    counts = (len(me.vertices), len(me.edges), len(me.polygons), len(ob.modifiers))
    bbox = (tuple(min(v.co for v in me.vertices)), tuple(max(v.co for v in me.vertices)))
    vsum = tuple(sum((v.co for v in me.vertices), Vector((0, 0, 0))))

    return (ob.name, counts, bbox, vsum)


def is_object_valid(ob):
    global contour_mesh_cache
    if 'valid' not in contour_mesh_cache:
        return False
    return contour_mesh_cache['valid'] == object_validation(ob)


def write_mesh_cache(orig_ob, tmp_ob, bme):
    print('writing mesh cache')
    global contour_mesh_cache
    clear_mesh_cache()
    contour_mesh_cache['valid'] = object_validation(orig_ob)
    contour_mesh_cache['bme'] = bme
    contour_mesh_cache['tmp'] = tmp_ob


def clear_mesh_cache():
    print('clearing mesh cache')

    global contour_mesh_cache

    if 'valid' in contour_mesh_cache and contour_mesh_cache['valid']:
        del contour_mesh_cache['valid']

    if 'bme' in contour_mesh_cache and contour_mesh_cache['bme']:
        bme_old = contour_mesh_cache['bme']
        bme_old.free()
        del contour_mesh_cache['bme']

    if 'tmp' in contour_mesh_cache and contour_mesh_cache['tmp']:
        old_obj = contour_mesh_cache['tmp']
        #context.scene.objects.unlink(self.tmp_ob)
        old_me = old_obj.data
        old_obj.user_clear()
        if old_obj and old_obj.name in bpy.data.objects:
            bpy.data.objects.remove(old_obj)
        if old_me and old_me.name in bpy.data.meshes:
            bpy.data.meshes.remove(old_me)
        del contour_mesh_cache['tmp']


class ContourToolsAddonPreferences(AddonPreferences):
    bl_idname = __name__

    simple_vert_inds = BoolProperty(
        name="Simple Inds",
        default=False,
        )

    vert_inds = BoolProperty(
        name="Vert Inds",
        description="Display indices of the raw contour verts",
        default=False,
        )

    show_verts = BoolProperty(
        name="Show Raw Verts",
        description="Display the raw contour verts",
        default=False,
        )

    show_edges = BoolProperty(
        name="Show Span Edges",
        description="Display the extracted mesh edges. Usually only turned off for debugging",
        default=True,
        )

    show_cut_indices = BoolProperty(
        name="Show Cut Indices",
        description="Display the order the operator stores cuts. Usually only turned on for debugging",
        default=False,
        )

    show_ring_edges = BoolProperty(
        name="Show Ring Edges",
        description="Display the extracted mesh edges. Usually only turned off for debugging",
        default=True,
        )

    draw_widget = BoolProperty(
        name="Draw Widget",
        description="Turn display of widget on or off",
        default=True,
        )

    debug = IntProperty(
        name="Debug Level",
        default=1,
        min=0,
        max=4,
        )

    show_backbone = BoolProperty(
        name="show_backbone",
        description="Show Cut Series Backbone",
        default=False)

    show_nodes = BoolProperty(
        name="show_nodes",
        description="Show Cut Nodes",
        default=False)

    show_ring_inds = BoolProperty(
        name="show_ring_inds",
        description="Show Ring Indices",
        default=False)

    show_axes = BoolProperty(
        name="show_axes",
        description="Show Cut Axes",
        default=False)

    show_debug = BoolProperty(
        name="Show Debug Settings",
        description="Show the debug settings, useful for troubleshooting",
        default=False,
        )

    vert_size = IntProperty(
        name="Vertex Size",
        default=4,
        min=1,
        max=10,
        )

    edge_thick = IntProperty(
        name="Edge Thickness",
        default=1,
        min=1,
        max=10,
        )

    theme = EnumProperty(
        items=[
            ('blue', 'Blue', 'Blue color scheme'),
            ('green', 'Green', 'Green color scheme'),
            ('orange', 'Orange', 'Orange color scheme'),
            ],
        name='theme',
        default='blue'
        )


    def rgba_to_float(r, g, b, a):
        return (r/255.0, g/255.0, b/255.0, a/255.0)

    theme_colors_active = {
        'blue': rgba_to_float(105, 246, 113, 255),
        'green': rgba_to_float(102, 165, 240, 255),
        'orange': rgba_to_float(102, 165, 240, 255)
    }
    theme_colors_mesh = {
        'blue': rgba_to_float(102, 165, 240, 255),
        'green': rgba_to_float(105, 246, 113, 255),
        'orange': rgba_to_float(254, 145, 0, 255)
    }

    raw_vert_size = IntProperty(
        name="Raw Vertex Size",
        default=1,
        min=1,
        max=10,
        )

    handle_size = IntProperty(
        name="Handle Vertex Size",
        default=8,
        min=1,
        max=10,
        )
 
    line_thick = IntProperty(
        name="Line Thickness",
        default=1,
        min=1,
        max=10,
        )

    stroke_thick = IntProperty(
        name="Stroke Thickness",
        description="Width of stroke lines drawn by user",
        default=1,
        min=1,
        max=10,
        )

    auto_align = BoolProperty(
        name="Automatically Align Vertices",
        description="Attempt to automatically align vertices in adjoining edgeloops. Improves outcome, but slows performance",
        default=True,
        )

    live_update = BoolProperty(
        name="Live Update",
        description="Will live update the mesh preview when transforming cut lines. Looks good, but can get slow on large meshes",
        default=True,
        )

    use_x_ray = BoolProperty(
        name="X-Ray",
        description='Enable X-Ray on Retopo-mesh upon creation',
        default=False,
        )

    use_perspective = BoolProperty(
        name="Use Perspective",
        description='Make non parallel cuts project from the same view to improve expected outcome',
        default=True,
        )

    new_method = BoolProperty(
        name="New Method",
        description="Use robust cutting, may be slower, more accurate on dense meshes",
        default=True,
        )

    # TODO  Theme this out nicely :-) 
    widget_color = FloatVectorProperty(name="Widget Color", description="Choose Widget color", min=0, max=1, default=(0,0,1), subtype="COLOR")
    widget_color2 = FloatVectorProperty(name="Widget Color", description="Choose Widget color", min=0, max=1, default=(1,0,0), subtype="COLOR")
    widget_color3 = FloatVectorProperty(name="Widget Color", description="Choose Widget color", min=0, max=1, default=(0,1,0), subtype="COLOR")
    widget_color4 = FloatVectorProperty(name="Widget Color", description="Choose Widget color", min=0, max=1, default=(0,0.2,.8), subtype="COLOR")
    widget_color5 = FloatVectorProperty(name="Widget Color", description="Choose Widget color", min=0, max=1, default=(.9,.1,0), subtype="COLOR")

    widget_radius = IntProperty(
        name="Widget Radius",
        description="Size of cutline widget radius",
        default=25,
        min=20,
        max=100,
        )

    widget_radius_inner = IntProperty(
        name="Widget Inner Radius",
        description="Size of cutline widget inner radius",
        default=10,
        min=5,
        max=30,
        )

    widget_thickness = IntProperty(
        name="Widget Line Thickness",
        description="Width of lines used to draw widget",
        default=2,
        min=1,
        max=10,
        )

    widget_thickness2 = IntProperty(
        name="Widget 2nd Line Thick",
        description="Width of lines used to draw widget",
        default=4,
        min=1,
        max=10,
        )

    arrow_size = IntProperty(
        name="Arrow Size",
        default=12,
        min=5,
        max=50,
        )   

    arrow_size2 = IntProperty(
        name="Translate Arrow Size",
        default=10,
        min=5,
        max=50,
        )

    vertex_count = IntProperty(
        name="Vertex Count",
        description="The Number of Vertices Per Edge Ring",
        default=10,
        min=3,
        max=250,
        )

    ring_count = IntProperty(
        name="Ring Count",
        description="The Number of Segments Per Guide Stroke",
        default=10,
        min=3,
        max=100,
        )

    cyclic = BoolProperty(
        name="Cyclic",
        description="Make contour loops cyclic",
        default=False)

    recover = BoolProperty(
        name="Recover",
        description="Recover strokes from last session",
        default=False)

    recover_clip = IntProperty(
        name="Recover Clip",
        description="Number of cuts to leave out, usually set to 0 or 1",
        default=1,
        min=0,
        max=10,
        )

    search_factor = FloatProperty(
        name="Search Factor",
        description="Factor of existing segment length to connect a new cut",
        default=5,
        min=0,
        max=30,
        )

    intersect_threshold = FloatProperty(
        name="Intersect Factor",
        description="Stringence for connecting new strokes",
        default=1.0,
        min=0.000001,
        max=1,
        )

    merge_threshold = FloatProperty(
        name="Intersect Factor",
        description="Distance below which to snap strokes together",
        default=1.0,
        min=0.000001,
        max=1,
        )

    cull_factor = IntProperty(
        name="Cull Factor",
        description="Fraction of screen drawn points to throw away. Bigger = less detail",
        default=4,
        min=1,
        max=10,
        )

    smooth_factor = IntProperty(
        name="Smooth Factor",
        description="Number of iterations to smooth drawn strokes",
        default=5,
        min=1,
        max=10,
        )

    feature_factor = IntProperty(
        name="Smooth Factor",
        description="Fraction of sketch bounding box to be considered feature. Bigger = More Detail",
        default=4,
        min=1,
        max=20,
        )

    extend_radius = IntProperty(
        name="Snap/Extend Radius",
        default=20,
        min=5,
        max=100,
        )

    undo_depth = IntProperty(
        name="Undo Depth",
        default=10,
        min=0,
        max=100,
        )

    
    def draw(self, context):
        layout = self.layout

        # Interaction Settings
        row = layout.row(align=True)
        row.prop(self, "auto_align")
        row.prop(self, "live_update")
        row.prop(self, "use_perspective")

        row = layout.row()
        row.prop(self, "use_x_ray", "Enable X-Ray at Mesh Creation")

        # Theme testing
        row = layout.row(align=True)
        row.prop(self, "theme", "Theme")

        # Visualization Settings
        box = layout.box().column(align=False)
        row = box.row()
        row.label(text="Stroke And Loop Settings")        

        row = box.row(align=False)
        row.prop(self, "handle_size", text="Handle Size")
        row.prop(self, "stroke_thick", text="Stroke Thickness")

        row = box.row(align=False)
        row.prop(self, "show_edges", text="Show Edge Loops")
        row.prop(self, "line_thick", text ="Edge Thickness")

        row = box.row(align=False)
        row.prop(self, "show_ring_edges", text="Show Edge Rings")
        row.prop(self, "vert_size")

        row = box.row(align=True)
        row.prop(self, "show_cut_indices", text = "Edge Indices")

        # Widget Settings
        box = layout.box().column(align=False)
        row = box.row()
        row.label(text="Widget Settings")

        row = box.row()
        row.prop(self,"draw_widget", text = "Display Widget")

        if self.draw_widget:
            row = box.row()
            row.prop(self, "widget_radius", text="Radius")
            row.prop(self,"widget_radius_inner", text="Active Radius")

            row = box.row()
            row.prop(self, "widget_thickness", text="Line Thickness")
            row.prop(self, "widget_thickness2", text="2nd Line Thickness")
            row.prop(self, "arrow_size", text="Arrow Size")
            row.prop(self, "arrow_size2", text="Translate Arrow Size")

            row = box.row()
            row.prop(self, "widget_color", text="Color 1")
            row.prop(self, "widget_color2", text="Color 2")
            row.prop(self, "widget_color3", text="Color 3")
            row.prop(self, "widget_color4", text="Color 4")
            row.prop(self, "widget_color5", text="Color 5")

        # Debug Settings
        box = layout.box().column(align=False)
        row = box.row()
        row.label(text="Debug Settings")

        row = box.row()
        row.prop(self, "show_debug", text="Show Debug Settings")

        if self.show_debug:
            row = box.row()
            row.prop(self, "new_method")
            row.prop(self, "debug")

            row = box.row()
            row.prop(self, "vert_inds", text="Show Vertex Indices")
            row.prop(self, "simple_vert_inds", text="Show Simple Indices")

            row = box.row()
            row.prop(self, "show_verts", text="Show Raw Vertices")
            row.prop(self, "raw_vert_size")

            row = box.row()
            row.prop(self, "show_backbone", text="Show Backbone")
            row.prop(self, "show_nodes", text="Show Cut Nodes")
            row.prop(self, "show_ring_inds", text="Show Ring Indices")


class CGCOOKIE_OT_retopo_contour_panel(bpy.types.Panel):
    '''Retopologize Forms with Contour Strokes'''
    bl_category = "Retopology"
    bl_label = "Contour Retopolgy"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'TOOLS'

    @classmethod
    def poll(cls, context):
        mode = bpy.context.mode
        obj = context.active_object
        return (obj and obj.type == 'MESH' and mode in ('OBJECT', 'EDIT_MESH'))


    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)

        cgc_contour = context.user_preferences.addons[AL.FolderName].preferences

        if 'EDIT' in context.mode and len(context.selected_objects) != 2:
            col.label(text='No 2nd Object!')

        col.operator("cgcookie.retop_contour", icon='IPO_LINEAR')
        col.prop(cgc_contour, "vertex_count")

        col = layout.column()
        col.label("Guide Mode:")
        col.prop(cgc_contour, "ring_count")

        # Commenting out for now until this is further improved and made to work again ###
        # row = box.row()
        # row.prop(cgc_contour, "cyclic")

        col = layout.column()
        col.label("Cache:")

        row = layout.row()
        row.prop(cgc_contour, "recover")

        if cgc_contour.recover:
            row.prop(cgc_contour, "recover_clip")

        row = layout.row()
        row.operator("cgcookie.clear_cache", text = "Clear Cache", icon = 'CANCEL')


class CGCOOKIE_OT_retopo_contour_menu(bpy.types.Menu):
    bl_label = "Retopology"
    bl_space_type = 'VIEW_3D'
    bl_idname = "object.retopology_menu"

    def draw(self, context):
        layout = self.layout

        layout.operator_context = 'INVOKE_DEFAULT'

        cgc_contour = context.user_preferences.addons[AL.FolderName].preferences

        layout.operator("cgcookie.retop_contour")


class CGCOOKIE_OT_retopo_cache_clear(bpy.types.Operator):
    '''Removes the temporary object and mesh data from the cache. Do this if you have altered your original form in any way'''
    bl_idname = "cgcookie.clear_cache"
    bl_label = "Clear Contour Cache"

    def execute(self,context):

        clear_mesh_cache()
        return {'FINISHED'}


def retopo_draw_callback(self, context):

    settings = context.user_preferences.addons[AL.FolderName].preferences

    stroke_color = settings.theme_colors_active[settings.theme]

    if (self.post_update or self.modal_state == 'NAVIGATING') and context.space_data.use_occlude_geometry:
        for path in self.cut_paths:
            path.update_visibility(context, self.original_form)
            for cut_line in path.cuts:
                cut_line.update_visibility(context, self.original_form)

        self.post_update = False

    for i, c_cut in enumerate(self.cut_lines):
        if self.widget_interaction and self.drag_target == c_cut:
            interact = True
        else:
            interact = False

        c_cut.draw(context, settings, three_dimensional=self.navigating, interacting=interact)

        if c_cut.verts_simple != [] and settings.show_cut_indices:
            loc = location_3d_to_region_2d(context.region, context.space_data.region_3d, c_cut.verts_simple[0])
            blf.position(0, loc[0], loc[1], 0)
            blf.draw(0, str(i))

    if self.cut_line_widget and settings.draw_widget:
        self.cut_line_widget.draw(context)

    if len(self.draw_cache):
        # Draw guide line
        common_drawing.draw_polyline_from_points(context, self.draw_cache, stroke_color, 2, "GL_LINE_STIPPLE")

    if len(self.cut_paths):
        for path in self.cut_paths:
            path.draw(context, path=True, nodes=settings.show_nodes, rings=True, follows=True, backbone=settings.show_backbone)

    if len(self.snap_circle):
        # Draw snap circle
        contour_utilities.draw_polyline_from_points(context, self.snap_circle, self.snap_color, 2, "GL_LINE_SMOOTH")


class CGCOOKIE_OT_retopo_contour(bpy.types.Operator):
    '''Draw Perpendicular Strokes to Retopologize Cylindrical Forms'''
    bl_idname = "cgcookie.retop_contour"
    bl_label = "Draw Contours"

    @classmethod
    def poll(cls, context):
        if context.mode not in {'EDIT_MESH','OBJECT'}:
            return False

        if context.active_object:
            if context.mode == 'EDIT_MESH':
                if len(context.selected_objects) > 1:
                    return True
                else:
                    return False
            else:
                return context.object.type == 'MESH'
        else:
            return False

    def hover_guide_mode(self, context, settings, event):
        '''
        handles mouse selection, hovering, highlighting
        and snapping when the mouse moves in guide
        mode
        '''

        stroke_color = settings.theme_colors_active[settings.theme]
        mesh_color = settings.theme_colors_mesh[settings.theme]

        # Identify hover target for highlighting
        if self.cut_paths != []:
            target_at_all = False
            breakout = False
            for path in self.cut_paths:
                if not path.select:
                    path.unhighlight(settings)
                for c_cut in path.cuts:                    
                    h_target = c_cut.active_element(context,event.mouse_region_x,event.mouse_region_y)
                    if h_target:
                        path.highlight(settings)
                        target_at_all = True
                        self.hover_target = path
                        breakout = True
                        break

                if breakout:
                    break

            if not target_at_all:
                self.hover_target = None

        # Assess snap points
        if self.cut_paths != [] and not self.force_new:
            rv3d = context.space_data.region_3d
            breakout = False
            snapped = False
            for path in self.cut_paths:

                end_cuts = []
                if not path.existing_head and len(path.cuts):
                    end_cuts.append(path.cuts[0])
                if not path.existing_tail and len(path.cuts):
                    end_cuts.append(path.cuts[-1])

                if path.existing_head and not len(path.cuts):
                    end_cuts.append(path.existing_head)

                for n, end_cut in enumerate(end_cuts):

                    # Potential verts to snap to
                    snaps = [v for i, v in enumerate(end_cut.verts_simple) if end_cut.verts_simple_visible[i]]
                    # The screen versions os those
                    screen_snaps = [location_3d_to_region_2d(context.region,rv3d,snap) for snap in snaps]

                    mouse = Vector((event.mouse_region_x,event.mouse_region_y))
                    dists = [(mouse - snap).length for snap in screen_snaps]

                    if len(dists):
                        best = min(dists)
                        if best < 2 * settings.extend_radius and best > 4:  #TODO unify selection mouse pixel radius.

                            best_vert = screen_snaps[dists.index(best)]
                            view_z = rv3d.view_rotation * Vector((0,0,1))
                            if view_z.dot(end_cut.plane_no) > -0.75 and view_z.dot(end_cut.plane_no) < 0.75:

                                imx = rv3d.view_matrix.inverted()
                                normal_3d = imx.transposed() * end_cut.plane_no
                                if n == 1 or len(end_cuts) == 1:
                                    normal_3d = -1 * normal_3d
                                screen_no = Vector((normal_3d[0],normal_3d[1]))
                                angle = math.atan2(screen_no[1],screen_no[0]) - 1/2 * math.pi
                                left = angle + math.pi
                                right =  angle
                                self.snap = [path, end_cut]

                                if end_cut.desc == 'CUT_LINE' and len(path.cuts) > 1:

                                    self.snap_circle = contour_utilities.pi_slice(best_vert[0], best_vert[1], settings.extend_radius, 0.1 * settings.extend_radius, left, right, 20, t_fan=True)
                                    self.snap_circle.append(self.snap_circle[0])
                                else:
                                    self.snap_circle = contour_utilities.simple_circle(best_vert[0], best_vert[1], settings.extend_radius, 20)
                                    self.snap_circle.append(self.snap_circle[0])

                                breakout = True
                                if best < settings.extend_radius:
                                    snapped = True
                                    self.snap_color = (stroke_color)

                                else:
                                    alpha = 1 - best/(2*settings.extend_radius)
                                    self.snap_color = (mesh_color)

                                break

                    if breakout:
                        break

            if not breakout:
                self.snap = []
                self.snap_circle = []


    def hover_loop_mode(self, context, settings, event):
        '''
        Handles mouse selection and hovering
        '''
        #identify hover target for highlighting
        if self.cut_paths != []:
            
            new_target = False
            target_at_all = False
            
            for path in self.cut_paths:
                for c_cut in path.cuts:
                    if not c_cut.select:
                        c_cut.unhighlight(settings) 
                    
                    h_target = c_cut.active_element(context,event.mouse_region_x,event.mouse_region_y)
                    if h_target:
                        c_cut.highlight(settings)
                        target_at_all = True
                         
                        if (h_target != self.hover_target) or (h_target.select and not self.cut_line_widget):
                            
                            self.hover_target = h_target
                            if self.hover_target.desc == 'CUT_LINE':

                                if self.hover_target.select:
                                    for possible_parent in self.cut_paths:
                                        if self.hover_target in possible_parent.cuts:
                                            parent_path = possible_parent
                                            break
                                            
                                    self.cut_line_widget = CutLineManipulatorWidget(context, 
                                                                                    settings,
                                                                                    self.original_form, self.bme,
                                                                                    self.hover_target,
                                                                                    parent_path,
                                                                                    event.mouse_region_x,
                                                                                    event.mouse_region_y)
                                    self.cut_line_widget.derive_screen(context)
                                
                                else:
                                    self.cut_line_widget = None
                            
                        else:
                            if self.cut_line_widget:
                                self.cut_line_widget.x = event.mouse_region_x
                                self.cut_line_widget.y = event.mouse_region_y
                                self.cut_line_widget.derive_screen(context)
                    #elif not c_cut.select:
                        #c_cut.geom_color = (settings.geom_rgb[0],settings.geom_rgb[1],settings.geom_rgb[2],1)          
            if not target_at_all:
                self.hover_target = None
                self.cut_line_widget = None


    def new_path_from_draw(self, context, settings):
        '''
        package all the steps needed to make a new path
        TODO: What if errors?
        '''
        path = ContourCutSeries(context, self.draw_cache,
                                    segments=settings.ring_count,
                                    ring_segments=settings.vertex_count,
                                    cull_factor=settings.cull_factor, 
                                    smooth_factor=settings.smooth_factor,
                                    feature_factor=settings.feature_factor)


        path.ray_cast_path(context, self.original_form)
        if len(path.raw_world) == 0:
            print('NO RAW PATH')
            return None
        path.find_knots()

        if self.snap != [] and not self.force_new:
            merge_series = self.snap[0]
            merge_ring = self.snap[1]

            path.snap_merge_into_other(merge_series, merge_ring, context, self.original_form, self.bme)

            return merge_series

        path.smooth_path(context, ob = self.original_form)
        path.create_cut_nodes(context)
        path.snap_to_object(self.original_form, raw=False, world=False, cuts=True)
        path.cuts_on_path(context, self.original_form, self.bme)
        path.connect_cuts_to_make_mesh(self.original_form)
        path.backbone_from_cuts(context, self.original_form, self.bme)
        path.update_visibility(context, self.original_form)
        if path.cuts:
            # TODO: should this ever be empty?
            path.cuts[-1].do_select(settings)
        
        self.cut_paths.append(path)

        return path


    def click_new_cut(self, context, settings, event):

        new_cut = ContourCutLine(event.mouse_region_x, event.mouse_region_y)

        for path in self.cut_paths:
            for cut in path.cuts:
                cut.deselect(settings)

        new_cut.do_select(settings)
        self.cut_lines.append(new_cut)

        return new_cut


    def release_place_cut(self, context, settings, event):
        self.selected.tail.x = event.mouse_region_x
        self.selected.tail.y = event.mouse_region_y

        width = Vector((self.selected.head.x, self.selected.head.y)) - Vector((self.selected.tail.x, self.selected.tail.y))

        # Prevent small errant strokes
        if width.length < 20: #TODO: Setting for minimum pixel width
            self.cut_lines.remove(self.selected)
            self.selected = None
            print('Placed cut is too short')
            return

        # Hit the mesh for the first time
        hit = self.selected.hit_object(context, self.original_form, method='VIEW')

        if not hit:
            self.cut_lines.remove(self.selected)
            self.selected = None
            print('Placed cut did not hit the mesh')
            return

        self.selected.cut_object(context, self.original_form, self.bme)
        self.selected.simplify_cross(self.segments)
        self.selected.update_com()
        self.selected.update_screen_coords(context)
        self.selected.head = None
        self.selected.tail = None

        if not len(self.selected.verts) or not len(self.selected.verts_simple):
            self.selected = None
            print('cut failure')  #TODO, header text message.

            return

        if settings.debug > 1:
            print('release_place_cut')
            print('len(self.cut_paths) = %d' % len(self.cut_paths))
            print('self.force_new = ' + str(self.force_new))

        if self.cut_paths != [] and not self.force_new:
            for path in self.cut_paths:
                if path.insert_new_cut(context, self.original_form, self.bme, self.selected, search=settings.search_factor):
                    # The cut belongs to the series now
                    path.connect_cuts_to_make_mesh(self.original_form)
                    path.update_visibility(context, self.original_form)
                    path.seg_lock = True
                    path.do_select(settings)
                    path.unhighlight(settings)
                    self.selected_path = path
                    self.cut_lines.remove(self.selected)
                    for other_path in self.cut_paths:
                        if other_path != self.selected_path:
                            other_path.deselect(settings)
                    # No need to search for more paths
                    return

        # Create a blank segment
        path = ContourCutSeries(context, [],
                        cull_factor=settings.cull_factor, 
                        smooth_factor=settings.smooth_factor,
                        feature_factor=settings.feature_factor)

        path.insert_new_cut(context, self.original_form, self.bme, self.selected, search=settings.search_factor)
        path.seg_lock = False  # Not locked yet...not until a 2nd cut is added in loop mode
        path.segments = 1
        path.ring_segments = len(self.selected.verts_simple)
        path.connect_cuts_to_make_mesh(self.original_form)
        path.update_visibility(context, self.original_form)

        for other_path in self.cut_paths:
            other_path.deselect(settings)

        self.cut_paths.append(path)
        self.selected_path = path
        path.do_select(settings)

        self.cut_lines.remove(self.selected)
        self.force_new = False


    def finish_mesh(self, context):
        back_to_edit = (context.mode == 'EDIT_MESH')

        # This is where all the magic happens
        print('pushing data into bmesh')
        for path in self.cut_paths:
            path.push_data_into_bmesh(context, self.destination_ob, self.dest_bme, self.original_form, self.dest_me)

        if back_to_edit:
            print('updating edit mesh')
            bmesh.update_edit_mesh(self.dest_me, tessface=False, destructive=True)

        else:
            # Write the data into the object
            print('write data into the object')
            self.dest_bme.to_mesh(self.dest_me)

            # Remember we created a new object
            print('link destination object')
            context.scene.objects.link(self.destination_ob)

            print('select and make active')
            self.destination_ob.select = True
            context.scene.objects.active = self.destination_ob

            if context.space_data.local_view:
                view_loc = context.space_data.region_3d.view_location.copy()
                view_rot = context.space_data.region_3d.view_rotation.copy()
                view_dist = context.space_data.region_3d.view_distance
                bpy.ops.view3d.localview()
                bpy.ops.view3d.localview()
                #context.space_data.region_3d.view_matrix = mx_copy
                context.space_data.region_3d.view_location = view_loc
                context.space_data.region_3d.view_rotation = view_rot
                context.space_data.region_3d.view_distance = view_dist
                context.space_data.region_3d.update()

        print('wrap up')
        context.area.header_text_set()
        contour_utilities.callback_cleanup(self,context)
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)

        print('finished mesh!')
        return {'FINISHED'}


    def widget_transform(self, context, settings, event):

        self.cut_line_widget.user_interaction(context, event.mouse_region_x, event.mouse_region_y, shift=event.shift)

        self.selected.cut_object(context, self.original_form, self.bme)
        self.selected.simplify_cross(self.selected_path.ring_segments)
        self.selected.update_com()
        self.selected_path.align_cut(self.selected, mode='BETWEEN', fine_grain=True)

        self.selected_path.connect_cuts_to_make_mesh(self.original_form)
        self.selected_path.update_visibility(context, self.original_form)

        self.temporary_message_start(context, 'WIDGET_TRANSFORM: ' + str(self.cut_line_widget.transform_mode))    


    def guide_arrow_shift(self, context, event):
        if event.type == 'LEFT_ARROW':         
            for cut in self.selected_path.cuts:
                cut.shift += 0.05
                cut.simplify_cross(self.selected_path.ring_segments)
        else:
            for cut in self.selected_path.cuts:
                cut.shift += -0.05
                cut.simplify_cross(self.selected_path.ring_segments)

        self.selected_path.connect_cuts_to_make_mesh(self.original_form)
        self.selected_path.update_visibility(context, self.original_form)  


    def loop_arrow_shift(self, context, event):    
        if event.type == 'LEFT_ARROW':
            self.selected.shift += 0.05

        else:
            self.selected.shift += -0.05

        self.selected.simplify_cross(self.selected_path.ring_segments)
        self.selected_path.connect_cuts_to_make_mesh(self.original_form)
        self.selected_path.update_backbone(context, self.original_form, self.bme, self.selected, insert=False)
        self.selected_path.update_visibility(context, self.original_form)

        self.temporary_message_start(context, self.mode +': Shift ' + str(self.selected.shift))


    def loop_align_modal(self, context, event):
        if not event.ctrl and not event.shift:
            act = 'BETWEEN'

        # Align ahead    
        elif event.ctrl and not event.shift:
            act = 'FORWARD'

        # Align behind    
        elif event.shift and not event.ctrl:
            act = 'BACKWARD'
 
        self.selected_path.align_cut(self.selected, mode=act, fine_grain=True)
        self.selected.simplify_cross(self.selected_path.ring_segments)

        self.selected_path.connect_cuts_to_make_mesh(self.original_form)
        self.selected_path.update_backbone(context, self.original_form, self.bme, self.selected, insert=False)
        self.selected_path.update_visibility(context, self.original_form)
        self.temporary_message_start(context, 'Align Loop: %s' % act)


    def loop_hotkey_modal(self,context,event):

        if self.hot_key == 'G':
            self.cut_line_widget = CutLineManipulatorWidget(context, 
                                                            self.settings,
                                                            self.original_form, self.bme,
                                                            self.selected,
                                                            self.selected_path,
                                                            event.mouse_region_x,event.mouse_region_y,
                                                            hotkey=self.hot_key)
            self.cut_line_widget.transform_mode = 'EDGE_SLIDE'

        elif self.hot_key == 'R':
            # TODO...if CoM is off screen, then what?
            screen_pivot = location_3d_to_region_2d(context.region,context.space_data.region_3d,self.selected.plane_com)
            self.cut_line_widget = CutLineManipulatorWidget(context, self.settings, 
                                                            self.original_form, self.bme,
                                                            self.selected,
                                                            self.selected_path,
                                                            screen_pivot[0],screen_pivot[1],
                                                            hotkey = self.hot_key)
            self.cut_line_widget.transform_mode = 'ROTATE_VIEW'

        self.cut_line_widget.initial_x = event.mouse_region_x
        self.cut_line_widget.initial_y = event.mouse_region_y
        self.cut_line_widget.derive_screen(context)


    def temporary_message_start(self, context, message):
        self.msg_start_time = time.time()
        if not self._timer:
            self._timer = context.window_manager.event_timer_add(0.1, context.window)
        
        context.area.header_text_set(text = message)    


    def modal(self, context, event):
        context.area.tag_redraw()
        settings = context.user_preferences.addons[AL.FolderName].preferences

        if event.type == 'Z' and event.ctrl and event.value == 'PRESS':
            self.temporary_message_start(context, "Undo Action")
            self.undo_action()

        # Check messages
        if event.type == 'TIMER':
            now = time.time()
            if now - self.msg_start_time > self.msg_duration:
                if self._timer:
                    context.window_manager.event_timer_remove(self._timer)
                    self._timer = None

                if self.mode == 'GUIDE':
                    context.area.header_text_set(text=self.guide_msg)
                else:
                    context.area.header_text_set(text=self.loop_msg)

        if self.modal_state == 'NAVIGATING':

            if (event.type in {'MOUSEMOVE',
                               'MIDDLEMOUSE', 
                                'NUMPAD_2', 
                                'NUMPAD_4', 
                                'NUMPAD_6',
                                'NUMPAD_8', 
                                'NUMPAD_1', 
                                'NUMPAD_3', 
                                'NUMPAD_5', 
                                'NUMPAD_7',
                                'NUMPAD_9'} and event.value == 'RELEASE'):

                self.modal_state = 'WAITING'
                self.post_update = True
                return {'PASS_THROUGH'}

            if (event.type in {'TRACKPADPAN', 'TRACKPADZOOM'} or event.type.startswith('NDOF_')):

                self.modal_state = 'WAITING'
                self.post_update = True 
                return {'PASS_THROUGH'}

        if self.mode == 'LOOP':

            if self.modal_state == 'WAITING':

                if (event.type in {'ESC','RIGHT_MOUSE'} and 
                    event.value == 'PRESS'):

                    context.area.header_text_set()
                    contour_utilities.callback_cleanup(self,context)
                    if self._timer:
                        context.window_manager.event_timer_remove(self._timer)

                    return {'CANCELLED'}

                elif (event.type == 'TAB' and 
                      event.value == 'PRESS'):

                    self.mode = 'GUIDE'
                    self.selected = None  #WHY?
                    if self.selected_path:
                        self.selected_path.highlight(settings)

                    if self._timer:
                        context.window_manager.event_timer_remove(self._timer)
                        self._timer = None

                    context.area.header_text_set(text=self.guide_msg)

                elif event.type == 'N' and event.value == 'PRESS':
                    self.force_new = self.force_new != True
                    #self.selected_path = None
                    self.snap = None

                    self.temporary_message_start(context, self.mode +': FORCE NEW: ' + str(self.force_new))
                    return {'RUNNING_MODAL'}

                elif (event.type in {'RET', 'NUMPAD_ENTER'} and 
                    event.value == 'PRESS'):

                    return self.finish_mesh(context)

                if event.type == 'MOUSEMOVE':
                    self.hover_loop_mode(context, settings, event)
                
                elif (event.type == 'C' and
                      event.value == 'PRESS'):

                    bpy.ops.view3d.view_center_cursor()
                    self.temporary_message_start(context, 'Center View to Cursor')
                    return {'RUNNING_MODAL'}

                elif event.type == 'S' and event.value == 'PRESS' and event.shift:
                    if self.selected:
                        context.scene.cursor_location = self.selected.plane_com
                        self.temporary_message_start(context, 'Cursor to selected loop or segment')

                # Navigation Keys
                elif (event.type in {'MIDDLEMOUSE', 
                                    'NUMPAD_2', 
                                    'NUMPAD_4', 
                                    'NUMPAD_6',
                                    'NUMPAD_8', 
                                    'NUMPAD_1', 
                                    'NUMPAD_3', 
                                    'NUMPAD_5', 
                                    'NUMPAD_7',
                                    'NUMPAD_9'} and event.value == 'PRESS'):

                    self.modal_state = 'NAVIGATING'
                    self.post_update = True
                    self.temporary_message_start(context, self.mode + ': NAVIGATING')

                    return {'PASS_THROUGH'}

                elif (event.type in {'TRACKPADPAN', 'TRACKPADZOOM'} or event.type.startswith('NDOF_')):

                    self.modal_state = 'NAVIGATING'
                    self.post_update = True
                    self.temporary_message_start(context, 'NAVIGATING')

                    return {'PASS_THROUGH'}

                # Zoom Keys
                elif (event.type in  {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and not 
                        (event.ctrl or event.shift)):

                    self.post_update = True
                    return{'PASS_THROUGH'}

                elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':

                    if self.hover_target and self.hover_target != self.selected:

                        self.selected = self.hover_target    
                        if not event.shift:
                            for path in self.cut_paths:
                                for cut in path.cuts:
                                        cut.deselect(settings)  
                                if self.selected in path.cuts and path != self.selected_path:
                                    path.do_select(settings)
                                    path.unhighlight(settings)
                                    self.selected_path = path
                                else:
                                    path.deselect(settings)

                        # Select the ring
                        self.hover_target.do_select(settings)

                    elif self.hover_target  and self.hover_target == self.selected:

                        self.create_undo_snapshot('WIDGET_TRANSFORM')
                        self.modal_state = 'WIDGET_TRANSFORM'
                        # Sometimes, there is not a widget from the hover?
                        self.cut_line_widget = CutLineManipulatorWidget(context, 
                                                                        settings,
                                                                        self.original_form, self.bme,
                                                                        self.hover_target,
                                                                        self.selected_path,
                                                                        event.mouse_region_x,
                                                                        event.mouse_region_y)
                        self.cut_line_widget.derive_screen(context)

                    else:
                        self.create_undo_snapshot('CUTTING')
                        self.modal_state = 'CUTTING'
                        self.temporary_message_start(context, self.mode + ': CUTTING')
                        # Make a new cut and handle it with self.selected
                        self.selected = self.click_new_cut(context, settings, event)

                    return {'RUNNING_MODAL'}

                if self.selected:
                    #print(event.type + " " + event.value)

                    #G -> HOTKEY
                    if event.type == 'G' and event.value == 'PRESS':

                        self.create_undo_snapshot('HOTKEY_TRANSFORM')
                        self.modal_state = 'HOTKEY_TRANSFORM'
                        self.hot_key = 'G'
                        self.loop_hotkey_modal(context,event)
                        self.temporary_message_start(context, self.mode + ':Hotkey Grab')
                        return {'RUNNING_MODAL'}
                    #R -> HOTKEY
                    if event.type == 'R' and event.value == 'PRESS':

                        self.create_undo_snapshot('HOTKEY_TRANSFORM')
                        self.modal_state = 'HOTKEY_TRANSFORM'
                        self.hot_key = 'R'
                        self.loop_hotkey_modal(context,event)
                        self.temporary_message_start(context, self.mode + ':Hotkey Rotate')
                        return {'RUNNING_MODAL'}

                    #X, DEL -> DELETE
                    elif event.type == 'X' and event.value == 'PRESS':

                        self.create_undo_snapshot('DELETE')
                        if len(self.selected_path.cuts) > 1 or (len(self.selected_path.cuts) == 1 and self.selected_path.existing_head):
                            self.selected_path.remove_cut(context, self.original_form, self.bme, self.selected)
                            self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                            self.selected_path.update_visibility(context, self.original_form)
                            self.selected_path.backbone_from_cuts(context, self.original_form, self.bme)

                        else:
                            self.cut_paths.remove(self.selected_path)
                            self.selected_path = None

                        self.selected = None
                        self.temporary_message_start(context, self.mode + ': DELETE')

                    #S -> CURSOR SELECTED CoM

                    #LEFT_ARROW, RIGHT_ARROW to shift
                    elif (event.type in {'LEFT_ARROW', 'RIGHT_ARROW'} and 
                          event.value == 'PRESS'):
                        self.create_undo_snapshot('LOOP_SHIFT') 
                        self.loop_arrow_shift(context,event)

                        return {'RUNNING_MODAL'}

                    elif event.type == 'A' and event.value == 'PRESS':
                        self.create_undo_snapshot('ALIGN')
                        self.loop_align_modal(context,event)

                        return {'RUNNING_MODAL'}

                    elif ((event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.ctrl) or
                          (event.type in {'NUMPAD_PLUS','NUMPAD_MINUS'} and event.value == 'PRESS') and event.ctrl):

                        self.create_undo_snapshot('RING_SEGMENTS')  
                        if not self.selected_path.ring_lock:
                            old_segments = self.selected_path.ring_segments
                            self.selected_path.ring_segments += 1 - 2 * (event.type == 'WHEELDOWNMOUSE' or event.type == 'NUMPAD_MINUS')
                            if self.selected_path.ring_segments < 3:
                                self.selected_path.ring_segments = 3

                            for cut in self.selected_path.cuts:
                                new_bulk_shift = round(cut.shift * old_segments/self.selected_path.ring_segments)
                                new_fine_shift = old_segments/self.selected_path.ring_segments * cut.shift - new_bulk_shift

                                new_shift =  self.selected_path.ring_segments/old_segments * cut.shift

                                print(new_shift - new_bulk_shift - new_fine_shift)
                                cut.shift = new_shift
                                cut.simplify_cross(self.selected_path.ring_segments)

                            self.selected_path.backbone_from_cuts(context, self.original_form, self.bme)    
                            self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                            self.selected_path.update_visibility(context, self.original_form)

                            self.temporary_message_start(context, self.mode +': RING SEGMENTS %i' %self.selected_path.ring_segments)
                            self.msg_start_time = time.time()
                        else:
                            self.temporary_message_start(context, self.mode +': RING SEGMENTS: Can not be changed.  Path Locked')

                        #else:
                            #let the user know the path is locked
                            #header message set

                        return {'RUNNING_MODAL'}
                    #if hover == selected:
                        #LEFTCLICK -> WIDGET

                return {'RUNNING_MODAL'}

            elif self.modal_state == 'CUTTING':

                if event.type == 'MOUSEMOVE':
                    #pass mouse coords to widget
                    x = str(event.mouse_region_x)
                    y = str(event.mouse_region_y)
                    message = self.mode + ':CUTTING: X: ' +  x + '  Y:  ' +  y
                    context.area.header_text_set(text=message)

                    self.selected.tail.x = event.mouse_region_x
                    self.selected.tail.y = event.mouse_region_y
                    #self.seleted.screen_to_world(context)

                    return {'RUNNING_MODAL'}

                elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':

                    #the new cut is created
                    #the new cut is assessed to be placed into an existing series
                    #the new cut is assessed to be an extension of selected gemometry
                    #the new cut is assessed to become the beginning of a new path
                    self.release_place_cut(context, settings, event)

                    # We return to waiting
                    self.modal_state = 'WAITING'
                    return {'RUNNING_MODAL'}

            elif self.modal_state == 'HOTKEY_TRANSFORM':
                if self.hot_key == 'G':
                    action = 'Grab'
                elif self.hot_key == 'R':
                    action = 'Rotate'

                if event.shift:
                        action = 'FINE CONTROL ' + action

                if event.type == 'MOUSEMOVE':
                    # Pass mouse coords to widget
                    x = str(event.mouse_region_x)
                    y = str(event.mouse_region_y)
                    message  = self.mode + ": " + action + ": X: " +  x + '  Y:  ' +  y
                    self.temporary_message_start(context, message)

                    # Widget.user_interaction
                    self.cut_line_widget.user_interaction(context, event.mouse_region_x,event.mouse_region_y)
                    self.selected.cut_object(context, self.original_form, self.bme)
                    self.selected.simplify_cross(self.selected_path.ring_segments)
                    self.selected.update_com()
                    self.selected_path.align_cut(self.selected, mode='BETWEEN', fine_grain=True)

                    self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                    self.selected_path.update_visibility(context, self.original_form)
                    return {'RUNNING_MODAL'}


                #LEFTMOUSE event.value == 'PRESS':#RET, ENTER
                if (event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and
                    event.value == 'PRESS'):
                    #confirm transform
                    #recut, align, visibility?, and update the segment
                    self.selected_path.update_backbone(context, self.original_form, self.bme, self.selected, insert=False)
                    self.modal_state = 'WAITING'
                    return {'RUNNING_MODAL'}

                if (event.type in {'ESC', 'RIGHTMOUSE'} and
                    event.value == 'PRESS'):
                    self.cut_line_widget.cancel_transform()
                    self.selected.cut_object(context, self.original_form, self.bme)
                    self.selected.simplify_cross(self.selected_path.ring_segments)
                    self.selected_path.align_cut(self.selected, mode='BETWEEN', fine_grain=True)
                    self.selected.update_com()

                    self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                    self.selected_path.update_visibility(context, self.original_form)
                    self.modal_state = 'WAITING'
                    return {'RUNNING_MODAL'}
            
            elif self.modal_state == 'WIDGET_TRANSFORM':

                # Mouse move
                if event.type == 'MOUSEMOVE':
                    if event.shift:
                        action = 'FINE WIDGET'
                    else:
                        action = 'WIDGET'

                    self.widget_transform(context, settings, event)

                    return {'RUNNING_MODAL'}

                elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    #destroy the widget
                    self.cut_line_widget = None
                    self.modal_state = 'WAITING'
                    self.selected_path.update_backbone(context, self.original_form, self.bme, self.selected, insert=False)

                    return {'RUNNING_MODAL'}

                elif  event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS' and self.hot_key:
                    self.cut_line_widget.cancel_transform()
                    self.selected.cut_object(context, self.original_form, self.bme)
                    self.selected.simplify_cross(self.selected_path.ring_segments)
                    self.selected.update_com()

                    self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                    self.selected_path.update_visibility(context, self.original_form)

                return {'RUNNING_MODAL'}

            return{'RUNNING_MODAL'}

        if self.mode == 'GUIDE':

            if self.modal_state == 'WAITING':
                # Navigation Keys
                if (event.type in {'MIDDLEMOUSE', 
                                    'NUMPAD_2', 
                                    'NUMPAD_4', 
                                    'NUMPAD_6',
                                    'NUMPAD_8', 
                                    'NUMPAD_1', 
                                    'NUMPAD_3', 
                                    'NUMPAD_5', 
                                    'NUMPAD_7',
                                    'NUMPAD_9'} and event.value == 'PRESS'):

                    self.modal_state = 'NAVIGATING'
                    self.post_update = True
                    self.temporary_message_start(context, 'NAVIGATING')

                    return {'PASS_THROUGH'}

                elif (event.type in {'ESC','RIGHT_MOUSE'} and 
                    event.value == 'PRESS'):

                    context.area.header_text_set()
                    contour_utilities.callback_cleanup(self,context)
                    if self._timer:
                        context.window_manager.event_timer_remove(self._timer)

                    return {'CANCELLED'}

                elif (event.type in {'RET', 'NUMPAD_ENTER'} and 
                    event.value == 'PRESS'):

                    return self.finish_mesh(context)

                elif (event.type in {'TRACKPADPAN', 'TRACKPADZOOM'} or event.type.startswith('NDOF_')):

                    self.modal_state = 'NAVIGATING'
                    self.post_update = True
                    self.temporary_message_start(context, 'NAVIGATING')

                    return {'PASS_THROUGH'}

                # ZOOM KEYS
                elif (event.type in  {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and not 
                        (event.ctrl or event.shift)):

                    self.post_update = True
                    self.temporary_message_start(context, 'ZOOM')
                    return{'PASS_THROUGH'}

                elif event.type == 'TAB' and event.value == 'PRESS':
                    self.mode = 'LOOP'
                    self.snap_circle = []

                    if self.selected_path:
                        self.selected_path.unhighlight(settings)

                    if self._timer:
                        context.window_manager.event_timer_remove(self._timer)
                        self._timer = None

                    context.area.header_text_set(text = self.loop_msg)
                    return {'RUNNING_MODAL'}

                elif event.type == 'C' and event.value == 'PRESS':
                    #center cursor
                    bpy.ops.view3d.view_center_cursor()
                    self.temporary_message_start(context, 'Center View to Cursor')
                    return {'RUNNING_MODAL'}

                elif event.type == 'N' and event.value == 'PRESS':
                    self.force_new = self.force_new != True
                    #self.selected_path = None
                    self.snap = None

                    self.temporary_message_start(context, self.mode +': FORCE NEW: ' + str(self.force_new))
                    return {'RUNNING_MODAL'}

                elif event.type == 'MOUSEMOVE':

                    self.hover_guide_mode(context, settings, event)

                    return {'RUNNING_MODAL'}

                elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                    if self.hover_target and self.hover_target.desc == 'CUT SERIES':
                        self.hover_target.do_select(settings)
                        self.selected_path = self.hover_target
                        
                        for path in self.cut_paths:
                            if path != self.hover_target:
                                path.deselect(settings)
                    else:
                        self.create_undo_snapshot('DRAW_PATH')
                        self.modal_state = 'DRAWING'
                        self.temporary_message_start(context, 'DRAWING')

                    return {'RUNNING_MODAL'}    

                if self.selected_path:

                    if event.type in {'X', 'DEL'} and event.value == 'PRESS':

                        self.create_undo_snapshot('DELETE')
                        self.cut_paths.remove(self.selected_path)
                        self.selected_path = None
                        self.modal_state = 'WAITING'
                        self.temporary_message_start(context, 'DELETED PATH')

                        return {'RUNNING_MODAL'}

                    elif (event.type in {'LEFT_ARROW', 'RIGHT_ARROW'} and 
                          event.value == 'PRESS'):

                        self.create_undo_snapshot('PATH_SHIFT')
                        self.guide_arrow_shift(context, event)

                        # Shift entire segment
                        self.temporary_message_start(context, 'Shift entire segment')
                        return {'RUNNING_MODAL'}

                    elif ((event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.ctrl) or
                          (event.type in {'NUMPAD_PLUS','NUMPAD_MINUS'} and event.value == 'PRESS')):

                        # If not selected_path.lock:
                        # TODO: path.locked
                        # TODO:  dont recalc the path when no change happens
                        if event.type in {'WHEELUPMOUSE','NUMPAD_PLUS'}:
                            if not self.selected_path.seg_lock:                            
                                self.create_undo_snapshot('PATH_SEGMENTS')
                                self.selected_path.segments += 1
                        elif event.type in {'WHEELDOWNMOUSE', 'NUMPAD_MINUS'} and self.selected_path.segments > 3:
                            if not self.selected_path.seg_lock:
                                self.create_undo_snapshot('PATH_SEGMENTS')
                                self.selected_path.segments -= 1

                        if not self.selected_path.seg_lock:
                            self.selected_path.create_cut_nodes(context)
                            self.selected_path.snap_to_object(self.original_form, raw = False, world = False, cuts = True)
                            self.selected_path.cuts_on_path(context, self.original_form, self.bme)
                            self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                            self.selected_path.update_visibility(context, self.original_form)
                            self.selected_path.backbone_from_cuts(context, self.original_form, self.bme)
                            #selected will hold old reference because all cuts are recreated (dumbly, it should just be the in between ones)
                            self.selected = self.selected_path.cuts[-1]
                            self.temporary_message_start(context, 'PATH SEGMENTS: %i' % self.selected_path.segments)

                        else:
                            self.temporary_message_start(context, 'PATH SEGMENTS: Path is locked, cannot adjust segments')
                        return {'RUNNING_MODAL'}

                    elif event.type == 'S' and event.value == 'PRESS':

                        if event.shift:
                            self.create_undo_snapshot('SMOOTH')
                            #path.smooth_normals
                            self.selected_path.average_normals(context, self.original_form, self.bme)
                            self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                            self.selected_path.backbone_from_cuts(context, self.original_form, self.bme)
                            self.temporary_message_start(context, 'Smooth normals based on drawn path')

                        elif event.ctrl:
                            self.create_undo_snapshot('SMOOTH')
                            #smooth CoM path
                            self.temporary_message_start(context, 'Smooth normals based on CoM path')
                            self.selected_path.smooth_normals_com(context, self.original_form, self.bme, iterations = 2)
                            self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                            self.selected_path.backbone_from_cuts(context, self.original_form, self.bme)
                        elif event.alt:
                            self.create_undo_snapshot('SMOOTH')
                            #path.interpolate_endpoints
                            self.temporary_message_start(context, 'Smoothly interpolate normals between the endpoints')
                            self.selected_path.interpolate_endpoints(context, self.original_form, self.bme)
                            self.selected_path.connect_cuts_to_make_mesh(self.original_form)
                            self.selected_path.backbone_from_cuts(context, self.original_form, self.bme)

                        else:
                            half = math.floor(len(self.selected_path.cuts)/2)

                            if math.fmod(len(self.selected_path.cuts), 2):  #5 segments is 6 rings
                                loc = 0.5 * (self.selected_path.cuts[half].plane_com + self.selected_path.cuts[half+1].plane_com)
                            else:
                                loc = self.selected_path.cuts[half].plane_com

                            context.scene.cursor_location = loc

                        return{'RUNNING_MODAL'}

            if self.modal_state == 'DRAWING':

                if event.type == 'MOUSEMOVE':
                    action = 'GUIDE MODE: Drawing'
                    x = str(event.mouse_region_x)
                    y = str(event.mouse_region_y)
                    # Record screen drawing
                    self.draw_cache.append((event.mouse_region_x,event.mouse_region_y))   

                    return {'RUNNING_MODAL'}

                if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                    if len(self.draw_cache) > 10:

                        for path in self.cut_paths:
                            path.deselect(settings)

                        self.selected_path  = self.new_path_from_draw(context, settings)
                        if self.selected_path:
                            self.selected_path.do_select(settings)
                            if self.selected_path.cuts:
                                self.selected = self.selected_path.cuts[-1]
                            else:
                                self.selected = None
                            if self.selected:
                                self.selected.do_select(settings)

                        self.drag = False  #TODO: is self.drag still needed?
                        self.force_new = False

                    self.draw_cache = []

                    self.modal_state = 'WAITING'
                    return{'RUNNING_MODAL'}

            return{'RUNNING_MODAL'}


    def create_undo_snapshot(self, action):
        '''
        saves data and operator state snapshot
        for undoing

        TODO:  perhaps pop/append are not fastest way
        deque?
        prepare a list and keep track of which entity to
        replace?
        '''

        repeated_actions = {'LOOP_SHIFT', 'PATH_SHIFT', 'PATH_SEGMENTS', 'LOOP_SEGMENTS'}

        if action in repeated_actions:
            if action == contour_undo_cache[-1][2]:
                print('repeatable...dont take snapshot')
                return

        print('undo: ' + action)    
        cut_data = copy.deepcopy(self.cut_paths)
        # Perhaps I don't even need to copy this?
        state = copy.deepcopy(ContourStatePreserver(self))
        contour_undo_cache.append((cut_data, state, action))

        if len(contour_undo_cache) > self.settings.undo_depth:
            contour_undo_cache.pop(0)
            

    def undo_action(self):

        if len(contour_undo_cache) > 0:
            cut_data, op_state, action = contour_undo_cache.pop()

            self.cut_paths = cut_data
            op_state.push_state(self)


    def invoke(self, context, event):
        # HINT you are in contours code
        # TODO Settings harmon CODE REVIEW
        settings = context.user_preferences.addons[AL.FolderName].preferences

        if context.space_data.viewport_shade in {'WIREFRAME','BOUNDBOX'}:
            self.report({'ERROR'}, 'Viewport shading must be at lease SOLID')
            return {'CANCELLED'}

        self.valid_cut_inds = []
        self.existing_loops = []

        # This is a cache for any cut line whose connectivity
        # has not been established.
        self.cut_lines = []

        # A list of all the cut paths (segments)
        self.cut_paths = []
        # A list to store screen coords when drawing
        self.draw_cache = []

        # TODO Settings harmony CODE REVIEW
        self.settings = settings

        # Default verts in a loop (spans)
        self.segments = settings.vertex_count
        # Default number of loops in a segment
        self.guide_cuts = settings.ring_count

        # If edit mode
        if context.mode == 'EDIT_MESH':

            # Retopo mesh is the active object
            self.destination_ob = context.object  #TODO:  Clarify destination_ob as retopo_on consistent with design doc

            # Get the destination mesh data
            self.dest_me = self.destination_ob.data

            # We will build this bmesh using from editmesh
            self.dest_bme = bmesh.from_edit_mesh(self.dest_me)

            # The selected object will be the original form
            # Or we wil pull the mesh cache
            target = [ob for ob in context.selected_objects if ob.name != context.object.name][0]

            # This is a simple set of recorded properties meant to help detect
            # If the mesh we are using is the same as the one in the cache.
            is_valid = is_object_valid(target)
            if is_valid:
                use_cache = True
                print('willing and able to use the cache!')
            else:
                use_cache = False  #later, we will double check for ngons and things
                clear_mesh_cache()
                self.original_form = target

            # Count and collect the selected edges if any
            ed_inds = [ed.index for ed in self.dest_bme.edges if ed.select]

            self.existing_loops = []
            if len(ed_inds):
                vert_loops = contour_utilities.edge_loops_from_bmedges(self.dest_bme, ed_inds)

                if len(vert_loops) > 1:
                    self.report({'WARNING'}, 'Only one edge loop will be used for extension')
                print('there are %i edge loops selected' % len(vert_loops))

                # For loop in vert_loops:
                # Until multi loops are supported, do this    
                loop = vert_loops[0]
                if loop[-1] != loop[0] and len(list(set(loop))) != len(loop):
                    self.report({'WARNING'},'Edge loop selection has extra parts!  Excluding this loop')

                else:
                    lverts = [self.dest_bme.verts[i] for i in loop]

                    existing_loop =ExistingVertList(context,
                                                    lverts, 
                                                    loop, 
                                                    self.destination_ob.matrix_world,
                                                    key_type='INDS')

                    # Make a blank path with just an existing head
                    path = ContourCutSeries(context, [],
                                    cull_factor=settings.cull_factor, 
                                    smooth_factor=settings.smooth_factor,
                                    feature_factor=settings.feature_factor)

                    path.existing_head = existing_loop
                    path.seg_lock = False
                    path.ring_lock = True
                    path.ring_segments = len(existing_loop.verts_simple)
                    path.connect_cuts_to_make_mesh(target)
                    path.update_visibility(context, target)

                    #path.update_visibility(context, self.original_form)

                    self.cut_paths.append(path)
                    self.existing_loops.append(existing_loop)

        elif context.mode == 'OBJECT':

            # Make the irrelevant variables None
            self.sel_edges = None
            self.sel_verts = None
            self.existing_cut = None

            # The active object will be the target
            target = context.object

            is_valid = is_object_valid(target)
            has_tmp = 'ContourTMP' in bpy.data.objects and bpy.data.objects['ContourTMP'].data

            if is_valid and has_tmp:
                use_cache = True
            else:
                use_cache = False
                self.original_form  = target #TODO:  Clarify original_form as reference_form consistent with design doc

            # No temp bmesh needed in object mode
            # We will create a new obeject
            self.tmp_bme = None

            # New blank mesh data
            self.dest_me = bpy.data.meshes.new(target.name + "_recontour")

            # New object to hold mesh data
            self.destination_ob = bpy.data.objects.new(target.name + "_recontour",self.dest_me) #this is an empty currently
            self.destination_ob.matrix_world = target.matrix_world
            self.destination_ob.update_tag()

            # Destination bmesh to operate on
            self.dest_bme = bmesh.new()
            self.dest_bme.from_mesh(self.dest_me)

        # Get the info about the original form
        #and convert it to a bmesh for fast connectivity info
        #or load the previous bme to save even more time

        if use_cache:
            start = time.time()
            print('the cache is valid for use!')

            self.bme = contour_mesh_cache['bme']
            print('loaded old bme in %f' % (time.time() - start))

            start = time.time()

            self.tmp_ob = contour_mesh_cache['tmp']
            print('loaded old tmp ob in %f' % (time.time() - start))

            if self.tmp_ob:
                self.original_form = self.tmp_ob
            else:
                self.original_form = target
 
        else:
            start = time.time()

            # Clear any old saved data
            clear_mesh_cache()

            me = self.original_form.to_mesh(scene=context.scene, apply_modifiers=True, settings='PREVIEW')
            me.update()

            self.bme = bmesh.new()
            self.bme.from_mesh(me)

            # Check for ngons, and if there are any...triangulate just the ngons
            #this mainly stems from the obj.ray_cast function returning triangulate
            #results and that it makes my cross section method easier.
            ngons = []
            for f in self.bme.faces:
                if len(f.verts) > 4:
                    ngons.append(f)
            if len(ngons) or len(self.original_form.modifiers) > 0:
                print('Ngons or modifiers detected this is a real hassle just so you know')

                if len(ngons):
                    #new_geom = bmesh.ops.triangulate(self.bme, faces = ngons, use_beauty = True)
                    new_geom = bmesh.ops.triangulate(self.bme, faces=ngons, quad_method=0, ngon_method=1)
                    new_faces = new_geom['faces']

                new_me = bpy.data.meshes.new('tmp_recontour_mesh')
                self.bme.to_mesh(new_me)
                new_me.update()

                self.tmp_ob = bpy.data.objects.new('ContourTMP', new_me)

                # I think this is needed to generate the data for raycasting
                #there may be some other way to update the object
                context.scene.objects.link(self.tmp_ob)
                self.tmp_ob.update_tag()
                context.scene.update() #this will slow things down
                context.scene.objects.unlink(self.tmp_ob)
                self.tmp_ob.matrix_world = self.original_form.matrix_world

                ###THIS IS A HUGELY IMPORTANT THING TO NOTICE!###
                #so maybe I need to make it more apparent or write it differnetly#
                #We are using a temporary duplicate to handle ray casting
                #and triangulation
                self.original_form = self.tmp_ob

            else:
                self.tmp_ob = None

            #store this stuff for next time.  We will most likely use it again
            #keep in mind, in some instances, tmp_ob is self.original orm
            #where as in others is it unique.  We want to use "target" here to
            #record validation because that is the the active or selected object
            #which is visible in the scene with a unique name.
            write_mesh_cache(target, self.tmp_ob, self.bme)
            print('derived new bme and any triangulations in %f' % (time.time() - start))

        message = "Segments: %i" % self.segments
        context.area.header_text_set(text=message)
 
        # Here is where we will cache verts edges and faces
        # Unti lthe user confirms and we output a real mesh.
        self.verts = []
        self.edges = []
        self.faces = []

        if settings.use_x_ray:
            self.orig_x_ray = self.destination_ob.show_x_ray
            self.destination_ob.show_x_ray = True     

        ####MODE, UI, DRAWING, and MODAL variables###
        self.mode = 'LOOP'
        #'LOOP' or 'GUIDE'

        self.modal_state = 'WAITING'

        # Does the user want to extend an existing cut or make a new segment
        self.force_new = False

        # Is the mouse clicked and held down
        self.drag = False
        self.navigating = False
        self.post_update = False

        # What is the user dragging..a cutline, a handle etc
        self.drag_target = None

        # Potential item for snapping in 
        self.snap = []
        self.snap_circle = []
        self.snap_color = (1, 0, 0, 1)

        # What is the mouse over top of currently
        self.hover_target = None
        # Keep track of selected cut_line and path
        self.selected = None   #TODO: Change this to selected_loop
        if len(self.cut_paths) == 0:
            self.selected_path = None   #TODO: change this to selected_segment
        else:
            print('there is a selected_path')
            self.selected_path = self.cut_paths[-1] #this would be an existing path from selected geom in editmode
        
        self.cut_line_widget = None  #An object of Class "CutLineManipulator" or None
        self.widget_interaction = False  #Being in the state of interacting with a widget o
        self.hot_key = None  #Keep track of which hotkey was pressed
        self.draw = False  #Being in the state of drawing a guide stroke
        
        self.loop_msg = 'LOOP MODE:  LMB: Select Stroke, X: Delete Sroke, , G: Translate, R: Rotate, Ctrl/Shift + A: Align, Shift + S: Cursor to Stroke, C: View to Cursor, N: Force New Segment, TAB: toggle Guide mode'
        self.guide_msg = 'GUIDE MODE: LMB to Draw or Select, Ctrl/Shift/ALT + S to smooth, WHEEL or +/- to increase/decrease segments, TAB: toggle Loop mode'
        context.area.header_text_set(self.loop_msg)

        if settings.recover and is_valid:
            print('loading cache!')
            self.undo_action()
        else:
            contour_undo_cache = []

        # Add in the draw callback and modal method
        self._handle = bpy.types.SpaceView3D.draw_handler_add(retopo_draw_callback, (self, context), 'WINDOW', 'POST_PIXEL')

        # Timer for temporary messages
        self._timer = None
        self.msg_start_time = time.time()
        self.msg_duration = 0.75

        context.window_manager.modal_handler_add(self)

        return {'RUNNING_MODAL'}

# Used to store keymaps for addon
addon_keymaps = []

# Registration
def register():
    bpy.utils.register_class(ContourToolsAddonPreferences)
    bpy.utils.register_class(CGCOOKIE_OT_retopo_contour_panel)
    bpy.utils.register_class(CGCOOKIE_OT_retopo_cache_clear)
    bpy.utils.register_class(CGCOOKIE_OT_retopo_contour)
    bpy.utils.register_class(CGCOOKIE_OT_retopo_contour_menu)

    # Create the addon hotkeys
    kc = bpy.context.window_manager.keyconfigs.addon

    # Create the mode switch menu hotkey
    km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
    kmi = km.keymap_items.new('wm.call_menu', 'V', 'PRESS', ctrl=True, shift=True)
    kmi.properties.name = 'object.retopology_menu' 
    kmi.active = True
    addon_keymaps.append((km, kmi))


# Unregistration
def unregister():
    clear_mesh_cache()
    bpy.utils.unregister_class(CGCOOKIE_OT_retopo_contour_menu)
    bpy.utils.unregister_class(CGCOOKIE_OT_retopo_contour)
    bpy.utils.unregister_class(CGCOOKIE_OT_retopo_cache_clear)
    bpy.utils.unregister_class(CGCOOKIE_OT_retopo_contour_panel)
    bpy.utils.unregister_class(ContourToolsAddonPreferences)

    # Remove addon hotkeys
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()
