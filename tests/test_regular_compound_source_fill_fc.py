"""Source/Boolean logic and actual page API regressions; not native OCC proof."""
import copy
from fractions import Fraction
import itertools
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / 'PDFVectorImporter', ROOT / 'PDFVectorImporter/src', ROOT / 'tests'):
    sys.path.insert(0, str(path))
import PDFImporterCore as core  # noqa: E402
import PDFSourceFill as fill  # noqa: E402
import test_clip_fill_degrade_fc as fixture  # noqa: E402


def rectangle(x, y, w, h, reverse=False):
    points = [(float(x),float(y)), (float(x+w),float(y)),
              (float(x+w),float(y+h)), (float(x),float(y+h))]
    if reverse:
        points.reverse()
    return tuple(('l',a,b) for a,b in zip(points,points[1:]+points[:1],strict=True))


def path(contours, even_odd=True, kind='f'):
    return dict(type=kind,fill=(.2,.6,.1),color=(.9,.1,.2) if kind=='fs' else None,
                fill_opacity=1.,stroke_opacity=1.,width=2.25,dashes='[3 2] 0',
                closePath=False,seqno=7,even_odd=even_odd,
                items=[command for contour in contours for command in contour])


def winding(points, x, y):
    """Independent ray-crossing reference at strict cell interiors."""
    result=0
    for a,b in zip(points,points[1:]+points[:1],strict=True):
        cross=(b[0]-a[0])*(y-a[1])-(x-a[0])*(b[1]-a[1])
        if a[1]<=y<b[1] and cross>0:result+=1
        elif b[1]<=y<a[1] and cross<0:result-=1
    return result


class Line:
    def __init__(self, points):
        a, b = points
        self.Location = a
        delta = tuple(y-x for x,y in zip(coords(a),coords(b),strict=True))
        length = math.sqrt(sum(v*v for v in delta))
        self.Direction = SimpleNamespace(**dict(zip(('x','y','z'),(v/length for v in delta),strict=True)))
    def isPeriodic(self):return False


class BezierCurve:
    Degree = 3
    def setPoles(self, points):self.points=points
    def getPoles(self):return self.points
    def getWeights(self):return [1.,1.,1.,1.]
    def isRational(self):return False
    def isPeriodic(self):return False
    def toShape(self):return Edge('c',self.points,self)


class Edge:
    def __init__(self,kind,points,curve=None):
        self.kind,self.points=kind,points
        self.Curve=curve or Line(points)
        self.Vertexes=[SimpleNamespace(Point=p) for p in (points[0],points[-1])]
        self.ShapeType,self.Orientation='Edge','Forward'
        matrix=[1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.,0.,0.,0.,0.,1.]
        self.Placement=SimpleNamespace(toMatrix=lambda:SimpleNamespace(A=matrix))
        # A positive sentinel for the pure cubic fixture; never native length
        # proof. Line lengths and trims follow the native line API exactly.
        self.Length=math.dist(coords(points[0]),coords(points[-1])) if kind=='l' else 1.
        self.FirstParameter,self.LastParameter=0.,self.Length if kind=='l' else 1.
    def isSame(self,other):return self is other
    def isNull(self):return False
    def isValid(self):return True


class Wire:
    def __init__(self,edges):self.Edges=edges
    def isNull(self):return not self.Edges
    def isClosed(self):return coords(self.Edges[0].points[0])==coords(self.Edges[-1].points[-1])
    def isValid(self):return all(coords(a.points[-1])==coords(b.points[0]) for a,b in zip(self.Edges,self.Edges[1:],strict=False))


def coords(p):return p.x,p.y,p.z


def vector(p):return SimpleNamespace(x=p[0],y=p[1],z=0.)


class Grid:
    def __init__(self,contours):
        self.x=sorted({p[0] for c in contours for cmd in c for p in cmd[1:]})
        self.y=sorted({p[1] for c in contours for cmd in c for p in cmd[1:]})
        self.cells=set(itertools.product(range(len(self.x)-1),range(len(self.y)-1)))
    def interior(self,cell):
        x,y=cell
        return (self.x[x]+self.x[x+1])/2,(self.y[y]+self.y[y+1])/2
    def area(self,cells):
        return sum((self.x[x+1]-self.x[x])*(self.y[y+1]-self.y[y]) for x,y in cells)


class Region:
    def __init__(self,cells,grid,wires=None,valid=True):
        self.cells,self.grid,self.valid=set(cells),grid,valid
        self.Area=grid.area(self.cells)
        self.Faces=[SimpleNamespace(Wires=wires or [])] if self.cells else []
        self.Edges=[] if wires is None else [edge for w in wires for edge in w.Edges]
    def isNull(self):return not self.cells
    def isValid(self):return self.valid
    def cut(self,other):return Region(self.cells-other.cells,self.grid)
    def common(self,other):return Region(self.cells&other.cells,self.grid)
    def fuse(self,other):return Region(self.cells|other.cells,self.grid)
    def removeSplitter(self):return self
    def copy(self):return Region(self.cells,self.grid)


class Kernel:
    """Exact compressed rectangular cells; Boolean API contract only."""
    def __init__(self,contours):
        self.grid=Grid(contours)
        self.native_faces=[]
    def LineSegment(self,a,b):return SimpleNamespace(toShape=lambda:Edge('l',[a,b]))
    BezierCurve=BezierCurve
    Wire=Wire
    def Face(self,wire):
        assert all(edge.kind=='l' for edge in wire.Edges)
        points=[(edge.points[0].x,edge.points[0].y) for edge in wire.Edges]
        cells={c for c in self.grid.cells if winding(points,*self.grid.interior(c))}
        result=Region(cells,self.grid,[wire]);self.native_faces.append(result);return result
    def makeCompound(self,wires):return fixture.Compound(wires)


def compose(contours,even_odd=True,kernel=None,cancel=lambda:None):
    kernel=kernel or Kernel(contours)
    return fill.build_compound_fill(contours,even_odd,kernel,vector,1.,cancel)


def reference_cells(contours,even_odd):
    grid=Grid(contours)
    points=[[command[1] for command in c] for c in contours]
    result=set()
    for cell in grid.cells:
        x,y=grid.interior(cell);number=sum(winding(p,x,y) for p in points)
        if (number%2 if even_odd else number!=0):result.add(cell)
    return result


def test_frozen_existing_ring_exact_source_and_hole(tmp_path):
    pdf = tmp_path / 'original compound ring.pdf'
    with core.fitz.open() as document:
        page = document.new_page(width=600, height=400)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
        path = page.new_shape()
        path.draw_rect(page.rect)
        path.draw_rect(core.fitz.Rect(290, 190, 310, 210))
        path.finish(color=None, fill=(0, 1, 0), even_odd=True)
        path.commit()
        document.save(pdf)
    with core.fitz.open(pdf) as doc:
        rows=doc[0].get_drawings()
    row=rows[1];before=copy.deepcopy(row)
    contours=fill.source_contours(row,core._parse_rect)
    assert [fill.signed_source_area(c) for c in contours]==[240000,400]
    shape,receipt=compose(contours)
    assert row==before and shape.Area==239600
    assert (1,1) not in shape.cells  # exact compressed cell [290,310]x[190,210]
    assert receipt['fill_rule']=='even_odd' and receipt['source_native_contour_bijection'] is True


@pytest.mark.parametrize('reverse_outer,reverse_hole,reverse_island',list(itertools.product([False,True],repeat=3)))
@pytest.mark.parametrize('even_odd',[False,True])
def test_nesting_islands_and_orientation_match_independent_integer_winding(reverse_outer,reverse_hole,reverse_island,even_odd):
    contours=[rectangle(0,0,20,20,reverse_outer),rectangle(3,3,14,14,reverse_hole),rectangle(7,7,6,6,reverse_island)]
    result,_=compose(contours,even_odd)
    assert result.cells==reference_cells(contours,even_odd)
    if even_odd:assert result.Area==240


@pytest.mark.parametrize('even_odd',[False,True])
@pytest.mark.parametrize('reverse',[False,True])
def test_crossing_overlap_is_not_mistaken_for_nested_holes(even_odd,reverse):
    contours=[rectangle(0,0,10,10),rectangle(5,3,10,10,reverse)]
    shape,_=compose(contours,even_odd)
    assert shape.cells==reference_cells(contours,even_odd)
    assert shape.Area==(130 if even_odd or reverse else 165)


@pytest.mark.parametrize('even_odd',[False,True])
def test_disjoint_touching_and_shared_edge_source_paints(even_odd):
    for contours in ([rectangle(0,0,10,10),rectangle(20,20,10,10)],
                     [rectangle(0,0,10,10),rectangle(10,0,10,10)],
                     [rectangle(0,0,10,10),rectangle(10,10,10,10)]):
        shape,_=compose(contours,even_odd)
        assert shape.cells==reference_cells(contours,even_odd) and shape.Area==200


@pytest.mark.parametrize('even_odd,reverse,empty',[(True,False,True),(True,True,True),(False,True,True),(False,False,False)])
def test_identical_contour_cancellation_is_truthfully_zero_ink(even_odd,reverse,empty):
    contours=[rectangle(0,0,10,10),rectangle(0,0,10,10,reverse)]
    shape,receipt=compose(contours,even_odd)
    assert (shape is None)==empty
    assert receipt['outcome']==('verified_zero_ink' if empty else 'native_painted_area')
    assert receipt['native_area']==(0 if empty else 100)


def test_nonzero_zero_winding_region_can_become_painted_again():
    contours=[rectangle(0,0,20,20),rectangle(0,0,20,20,True),rectangle(3,3,4,4)]
    shape,_=compose(contours,False)
    assert shape.Area==16 and shape.cells==reference_cells(contours,False)


def test_exact_cubic_area_and_every_original_control_without_tessellation():
    curve=(('c',(0.,0.),(0.,1.),(1.,1.),(1.,0.)),('l',(1.,0.),(0.,0.)))
    assert fill.signed_source_area(curve)==Fraction(-3,5)
    reverse=(('l',(0.,0.),(1.,0.)),('c',(1.,0.),(1.,1.),(0.,1.),(0.,0.)))
    assert fill.signed_source_area(reverse)==Fraction(3,5)
    native_calls=[]
    class CurveKernel:
        def LineSegment(self,a,b):return SimpleNamespace(toShape=lambda:Edge('l',[a,b]))
        class BezierCurve(BezierCurve):
            def setPoles(self,points):super().setPoles(points);native_calls.append([coords(p) for p in points])
        Wire=Wire
        def Face(self,wire):
            return SimpleNamespace(Area=.6,Faces=[SimpleNamespace(Wires=[wire])],isNull=lambda:False,isValid=lambda:True)
    face,sign=fill._native_face(curve,CurveKernel(),vector,1.,lambda:None)
    assert face.Area==.6 and sign==-1
    assert native_calls==[[(0,0,0),(0,1,0),(1,1,0),(1,0,0)]]


def test_closed_loop_restart_at_same_endpoint_keeps_both_fill_contours():
    contour=rectangle(0,0,10,10)
    row=path([contour,contour])
    result=fill.source_contours(row,core._parse_rect)
    assert result==(contour,contour)
    shape,receipt=compose(result)
    assert shape is None and receipt['outcome']=='verified_zero_ink'


def test_exact_simple_cubic_boundary_proof_and_curve_reversal():
    arch=(('c',(0.,0.),(0.,1.),(1.,1.),(1.,0.)),('l',(1.,0.),(0.,0.)))
    fill._prove_simple(arch,lambda:None)
    k=.5522847498307936
    circle=(('c',(1.,0.),(1.,k),(k,1.),(0.,1.)),
            ('c',(0.,1.),(-k,1.),(-1.,k),(-1.,0.)),
            ('c',(-1.,0.),(-1.,-k),(-k,-1.),(0.,-1.)),
            ('c',(0.,-1.),(k,-1.),(1.,-k),(1.,0.)))
    fill._prove_simple(circle,lambda:None)
    reversed_circle=tuple((c[0],*reversed(c[1:])) for c in reversed(circle))
    fill._prove_simple(reversed_circle,lambda:None)
    assert fill.signed_source_area(circle)==-fill.signed_source_area(reversed_circle)


@pytest.mark.parametrize('commands',[
    # Bow tie, repeated nonadjacent contact, overlap, and a looping cubic.
    (('l',(0.,0.),(2.,2.)),('l',(2.,2.),(0.,2.)),
     ('l',(0.,2.),(2.,0.)),('l',(2.,0.),(0.,0.))),
    (('l',(0.,0.),(2.,0.)),('l',(2.,0.),(1.,1.)),
     ('l',(1.,1.),(1.,0.)),('l',(1.,0.),(0.,0.))),
    (('l',(0.,0.),(2.,0.)),('l',(2.,0.),(1.,0.)),('l',(1.,0.),(0.,0.))),
    (('c',(0.,0.),(2.,2.),(-2.,2.),(0.,0.)),),
    # Injective cubic crossing the closing line in its interior.
    (('c',(0.,0.),(0.,1.),(1.,-1.),(1.,0.)),('l',(1.,0.),(0.,0.))),
])
def test_unproved_or_self_crossing_single_boundary_fails_before_native_shape(commands):
    calls=[]
    class NoKernel:
        def LineSegment(self,*args):calls.append(args);raise AssertionError('native shape started')
    with pytest.raises(fill.SourceFillError):
        fill._native_face(commands,NoKernel(),vector,1.,lambda:None)
    assert not calls


def test_disconnected_open_contours_fill_close_without_source_mutation():
    row=path([[('l',(0.,0.),(4.,0.)),('l',(4.,0.),(2.,3.))],
              [('l',(10.,0.),(14.,0.)),('l',(14.,0.),(12.,3.))]],kind='fs')
    before=copy.deepcopy(row);contours=fill.source_contours(row,core._parse_rect)
    assert len(contours)==2 and contours[0][-1]==('l',(2.,3.),(0.,0.))
    assert contours[1][-1]==('l',(12.,3.),(10.,0.)) and row==before


@pytest.mark.parametrize('mutation',['missing_rule','unknown_command','nonfinite','invalid_point','bool_point'])
def test_unproved_source_is_refused_instead_of_independent_opaque_faces(mutation):
    row=path([rectangle(0,0,10,10),rectangle(2,2,6,6)])
    if mutation=='missing_rule':row['even_odd']=1
    elif mutation=='unknown_command':row['items'].append(('v',(0,0),(2,2),(3,4)))
    elif mutation=='nonfinite':row['items'][0]=('l',(float('nan'),0),(10,0))
    elif mutation=='invalid_point':row['items'][0]=('l',(0,0,1),(10,0))
    else:row['items'][0]=('l',(True,0),(10,0))
    with pytest.raises(fill.SourceFillError):fill.source_contours(row,core._parse_rect)


@pytest.mark.parametrize('fault',['invalid_face','wrong_area','missing_boundary','extra_wire','changed_endpoint','boolean_failure','invalid_boolean','missing_kernel_method','boolean_area_loss'])
def test_native_errors_and_lost_contour_bijection_cannot_pass(fault):
    contours=[rectangle(0,0,10,10),rectangle(2,2,6,6)]
    kernel=Kernel(contours)
    original=kernel.Face
    def face(wire):
        result=original(wire)
        if fault=='invalid_face':result.valid=False
        elif fault=='wrong_area':result.Area+=1
        elif fault=='missing_boundary':result.Faces[0].Wires[0].Edges=result.Faces[0].Wires[0].Edges[:-1]
        elif fault=='extra_wire':result.Faces[0].Wires.append(wire)
        elif fault=='boolean_failure':result.cut=lambda other:(_ for _ in ()).throw(RuntimeError('kernel refused'))
        elif fault=='invalid_boolean':result.cut=lambda other:Region(result.cells,result.grid,valid=False)
        elif fault=='missing_kernel_method':result.common=None
        elif fault=='boolean_area_loss':result.cut=lambda other:Region(set(),result.grid)
        return result
    kernel.Face=face
    if fault=='changed_endpoint':
        def line(a,b):return SimpleNamespace(toShape=lambda:Edge('l',[vector((a.x+1,a.y)),b]))
        kernel.LineSegment=line
    with pytest.raises(fill.SourceFillError):compose(contours,kernel=kernel)


def test_cancel_exception_remains_exact_and_creates_no_native_host_object():
    contours=[rectangle(0,0,10,10),rectangle(2,2,6,6)]
    error=core.ImportCancelled('operator cancelled');calls=[]
    def cancel():
        calls.append(1)
        if len(calls)==6:raise error
    with pytest.raises(core.ImportCancelled) as caught:compose(contours,cancel=cancel)
    assert caught.value is error


def page_host(monkeypatch,contours):
    fixture.host.__wrapped__(monkeypatch)
    transformed=[tuple((command[0],*((p[0],100.-p[1]) for p in command[1:]))
                       for command in c) for c in contours]
    monkeypatch.setattr(core,'Part',Kernel(transformed))
    styles=[]
    def style(obj,stroke,color,width,dashes,opts):
        styles.append((obj.Name,stroke,color,width,dashes))
    monkeypatch.setattr(core,'_apply_style',style)
    return styles


@pytest.mark.parametrize('kind',['f','fs'])
@pytest.mark.parametrize('cancel_fill',[False,True])
@pytest.mark.parametrize('hatch_faces',[False,True])
def test_actual_page_API_compound_fill_fs_stroke_and_zero_ink(monkeypatch,kind,cancel_fill,hatch_faces):
    outer=rectangle(10,10,20,20)
    inner=outer if cancel_fill else rectangle(15,15,10,10)
    contours=[outer,inner];styles=page_host(monkeypatch,contours)
    row=path(contours,kind=kind)
    row['items']=[('re',core.fitz.Rect(10,10,30,30),1),
                  ('re',core.fitz.Rect(10,10,30,30) if cancel_fill else core.fitz.Rect(15,15,25,25),1)]
    before=copy.deepcopy(row)
    opts=fixture.options(hatch_to_faces=hatch_faces,make_faces=True,compound_batch_size=0,import_text=False,scale_to_mm=False)
    doc,opts,_=fixture.import_page([row],opts)
    fills=doc.named('SourceCompoundFill');wires=doc.named('Wire')
    assert not doc.named('Face') and row==before
    assert len(fills)==(0 if cancel_fill else 1)
    assert len(wires)==(0 if kind=='f' else 2)
    delivery=opts._report_extra['source_compound_fill_delivery']
    assert len(delivery)==1 and delivery[0]['source_page']==1 and delivery[0]['source_paint_order']==7
    assert delivery[0]['outcome']==('verified_zero_ink' if cancel_fill else 'native_painted_area')
    if fills:
        assert fills[0].Shape.Area==300
        assert json.loads(fills[0].PDFSourceCompoundFillJSON)==delivery[0]
        assert styles[0][1:]==(None,row['fill'],None,None)
    if kind=='fs':
        assert styles[-1][1:]==(row['color'],None,2.25,[3.,2.])
        assert all(len(wire.Shape.Edges)==4 for wire in wires)
        actual=[[(p.x,100.-p.y) for edge in w.Shape.Edges for p in edge.points] for w in wires]
        assert actual==[[p for cmd in c for p in cmd[1:]] for c in contours]


@pytest.mark.parametrize('cancel_fill',[False,True])
def test_optional_model3D_routes_only_composed_paint_to_original_extruder(monkeypatch,cancel_fill):
    outer=rectangle(10,10,20,20)
    inner=outer if cancel_fill else rectangle(15,15,10,10)
    contours=[outer,inner]
    page_host(monkeypatch,contours)
    row=path(contours,kind='fs')
    row['items']=[('re',core.fitz.Rect(10,10,30,30),1),
                  ('re',core.fitz.Rect(10,10,30,30) if cancel_fill else core.fitz.Rect(15,15,25,25),1)]
    policies=[];extrusions=[]
    def policy(opts,**fields):policies.append(fields);return True
    def extrude(obj,opts):extrusions.append((obj.Name,obj.Shape.Area));return True
    monkeypatch.setattr(core,'_model3d_should_extrude',policy)
    monkeypatch.setattr(core,'_extrude_model3d_obj',extrude)
    opts=fixture.options(hatch_to_faces=True,compound_batch_size=0,scale_to_mm=False)
    doc,opts,_=fixture.import_page([row],opts)
    assert len(policies)==(0 if cancel_fill else 1)
    assert len(doc.named('PDF_3D_Solid'))==(0 if cancel_fill else 1)
    assert extrusions==([] if cancel_fill else [(doc.named('PDF_3D_Solid')[0].Name,300.)])
    assert len(doc.named('Wire'))==2
    if policies:assert policies[0]['face_area']==300 and policies[0]['is_closed'] is True


@pytest.mark.parametrize('method',['Wire','LineSegment'])
def test_missing_native_constructor_is_a_typed_delivery_refusal(method):
    contours=[rectangle(0,0,10,10),rectangle(2,2,6,6)]
    kernel=Kernel(contours)
    setattr(kernel,method,None)
    with pytest.raises(fill.SourceFillError):compose(contours,kernel=kernel)


def run_page_wrapper(monkeypatch, tmp_path, contours, opts, document):
    source = tmp_path/'source.pdf'
    doc=core.fitz.open();page=doc.new_page(width=200,height=100);drawing=page.new_shape()
    for contour in contours:
        points=[command[1] for command in contour]
        drawing.draw_polyline(points+[points[0]])
    drawing.finish(color=None,fill=(.2,.6,.1),even_odd=True);drawing.commit();doc.save(source);doc.close()
    with core.fitz.open(source) as original:
        rows=original[0].get_drawings(extended=True)
    from pdfcadcore import fitz_loader
    monkeypatch.setattr(fitz_loader,'safe_open',lambda path:fixture.Pdf(fixture.Page(rows)))
    monkeypatch.setattr(core,'_ensure_doc',lambda:document)
    return core.import_pdf_page(str(source),1,opts,autofit=False)


def test_actual_page_cancellation_rolls_back_shapes_and_delivery_telemetry(monkeypatch,tmp_path):
    contours=[rectangle(10,10,20,20),rectangle(15,15,10,10)]
    page_host(monkeypatch,contours)
    opts=fixture.options(hatch_to_faces=True,compound_batch_size=0,scale_to_mm=False)
    opts.progress_callback=lambda event:False if event['label']=='Processing compound source fill...' else None
    document=fixture.Document()
    monkeypatch.setattr(core,'_recompute_page_if_needed',lambda *args:None)
    with pytest.raises(core.ImportCancelled):
        run_page_wrapper(monkeypatch,tmp_path,contours,opts,document)
    assert not document.Objects and opts.import_status=='cancelled'
    assert not opts._report_extra.get('source_compound_fill_delivery')


def test_page_geometry_failure_cleans_prior_owned_objects_and_is_not_READY(monkeypatch,tmp_path):
    contours=[rectangle(10,10,20,20),rectangle(15,15,10,10)]
    page_host(monkeypatch,contours)
    kernel=core.Part;original=kernel.Face
    def invalid(wire):
        result=original(wire);result.valid=False;return result
    kernel.Face=invalid
    opts=fixture.options(hatch_to_faces=True,compound_batch_size=0,scale_to_mm=False)
    document=fixture.Document()
    with pytest.raises(core.DrawingGeometryFailure) as caught:
        run_page_wrapper(monkeypatch,tmp_path,contours,opts,document)
    assert not document.Objects and opts.import_status=='failed'
    assert not opts._report_extra.get('source_compound_fill_delivery')
    report=tmp_path/'failure-report.json'
    opts.import_report_path=str(report)
    core._write_terminal_representation_failure_report(
        pdf_path=str(tmp_path/'source.pdf'),opts=opts,pages_imported=0,total_pages=1,
        elapsed_ms=1.,failure=caught.value)
    result=json.loads(report.read_text())
    assert result['extra']['result_status']=='failed'
    assert result['extra']['import_contract_ready']['ready'] is False


def test_original_clipped_helper_is_byte_and_AST_unchanged():
    baseline=ROOT.parent/'freecad-session-viewprovider-v1/PDFVectorImporter/src/PDFImporterCore.py'
    import ast
    def get(p):
        source=p.read_text(encoding='utf-8-sig');node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='_compound_clip_fill_shape')
        return ast.get_source_segment(source,node),ast.dump(node,include_attributes=False)
    assert get(baseline)==get(ROOT/'PDFVectorImporter/src/PDFImporterCore.py')
