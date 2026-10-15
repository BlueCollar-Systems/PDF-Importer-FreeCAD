"""Paper is an explicit, source-bound view aid, never generated CAD geometry."""
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'PDFVectorImporter/src'))
import PDFPaperDisplay as paper


def test_paper_creation_is_view_only_and_keeps_exact_page_bounds():
    class Object:
        ViewObject = None
        def addProperty(self,*_args):pass
        def setEditorMode(self,*_args):pass
    created=[]
    def add(kind,name):
        obj=Object();created.append((kind,name,obj));return obj
    doc=SimpleNamespace(addObject=add)
    corners=[(0,0,0),(1219.2,0,0),(1219.2,914.4,0),(0,914.4,0)]
    obj=paper.create_paper(doc,doc,page_number=1,source_sha256='a'*64,corners=corners)
    assert created[0][:2]==('App::FeaturePython','PDF_Paper')
    assert not hasattr(obj,'Shape')
    import json
    assert json.loads(obj.PDFPaperDisplayJSON)['corners_mm']==[list(p) for p in corners]


def test_view_box_is_the_placed_sheet_not_an_off_sheet_stroke():
    first = paper.placed_sheet_box([(0, 0, 0), (216, 0, 0), (216, 279, 0), (0, 279, 0)])
    second = paper.placed_sheet_box(
        [(0, 0, 0), (216, 0, 0), (216, 279, 0), (0, 279, 0)],
        origin=(0, -340),
    )
    box = paper.union_sheet_boxes([first, second])
    assert box[0] == 0 and box[2] == 216
    assert box[1] == -340 and box[3] == 279


@pytest.mark.parametrize('corners,depth', [([(0,0,0)]*4,-1),
    ([(0,0,0),(1,0,1),(1,1,0),(0,1,0)],-1),
    ([(0,0,0),(1,0,0),(1,1,0),(0,1,0)],1)])
def test_paper_rejects_invalid_source_plane(corners,depth):
    with pytest.raises(ValueError):
        paper.validate({'schema':paper.SCHEMA,'source_sha256':'a'*64,'corners_mm':corners,'display_z_mm':depth})


@pytest.fixture
def native_paper(monkeypatch):
    """Native-like one-shot Proxy attachment and independent display enumeration."""
    import itertools
    import json
    pointers = itertools.count(100)

    class Field:
        def __init__(self, value=None): self.value=value
        def setValue(self, *value): self.value=value[0] if len(value)==1 else value
        def setValues(self, value): self.value=list(value)
        def getValue(self): return self.value
        def getNum(self): return len(self.value)
        def __getitem__(self, i): return self.value[i]

    class Node:
        def __init__(self, kind):
            self.kind=kind;self.this=next(pointers);self.name='';self.children=[]
            self.translation=Field();self.rotation=Field();self.point=Field([]);self.numVertices=Field([])
            for name in ('diffuseColor','ambientColor','specularColor','emissiveColor'):
                setattr(self,name,Field())
        def setName(self,name): self.name=name
        def getName(self): return self.name
        def getTypeId(self): return SimpleNamespace(getName=lambda:self.kind)
        def isOfType(self,kind): return self.kind==kind
        def setOverride(self,value): self.override=value
        def addChild(self,node): self.children.append(node)
        def getNumChildren(self): return len(self.children)
        def getChild(self,i): return self.children[i]

    def factory(kind, **constants):
        class NativeNode(Node):
            def __init__(self): super().__init__(kind)
            @staticmethod
            def getClassTypeId(): return kind
        for name,value in constants.items():setattr(NativeNode,name,value)
        return NativeNode

    coin=SimpleNamespace(
        SoSeparator=factory('Separator'),SoTransform=factory('Transform'),
        SoPickStyle=factory('PickStyle',UNPICKABLE=2),SoMaterial=factory('Material'),
        SoDrawStyle=factory('DrawStyle',FILLED=0),SoCoordinate3=factory('Coordinate3'),
        SoFaceSet=factory('FaceSet'),
    )
    monkeypatch.setitem(sys.modules,'pivy',SimpleNamespace(coin=coin))

    class View:
        def __init__(self):
            self._proxy=None;self.native_attached=True;self.Visibility=True
            self._mode='';self.enums=[''];self.masks={};self.proxy_assignments=0
            self.SwitchNode=Node('Switch');self.SwitchNode.whichChild=Field(-1)
        @property
        def Proxy(self):return self._proxy
        @Proxy.setter
        def Proxy(self,value):
            self._proxy=value;self.proxy_assignments+=1
            if not self.native_attached:
                self.native_attached=True;value.attach(self)
        @property
        def DisplayMode(self):return self._mode
        @DisplayMode.setter
        def DisplayMode(self,value):
            if isinstance(value,list):
                self.enums=list(value);value=self._mode if self._mode in value else value[0]
            if value not in self.enums:raise ValueError('Display mode is not part of native enumeration')
            self._mode=value
            self.SwitchNode.whichChild.setValue(self.masks.get(value,-1) if self.Visibility else -1)
        def addDisplayMode(self,node,name):
            self.masks[name]=self.SwitchNode.getNumChildren();self.SwitchNode.addChild(node)
        def listDisplayModes(self):return self.Proxy.getDisplayModes(self)

    view=View()
    data={'schema':paper.SCHEMA,'source_sha256':'a'*64,'corners_mm':[[0,0,0],[20,0,0],[20,10,0],[0,10,0]],'display_z_mm':-1.}
    obj=SimpleNamespace(PDFPaperDisplayJSON=json.dumps(data),ViewObject=view,
        Placement=SimpleNamespace(Base=(3.,4.,0.),Rotation=SimpleNamespace(Q=(0.,0.,0.,1.))))
    view.Object=obj
    return SimpleNamespace(coin=coin,obj=obj,view=view,doc=SimpleNamespace(Objects=[obj]),Node=Node)


def test_restore_initializes_deserialized_proxy_and_real_empty_native_mode(native_paper):
    h=native_paper;proxy=paper.ViewProviderPaper.__new__(paper.ViewProviderPaper)
    h.view.Proxy=proxy
    assert not vars(proxy) and h.view.DisplayMode==''
    assert paper.restore_document_paper(h.doc)==1
    assert h.view.Proxy.view is h.view and h.view.DisplayMode=='Paper'
    assert h.view.SwitchNode.getNumChildren()==1
    assert h.view.SwitchNode.getChild(h.view.SwitchNode.whichChild.getValue()) is h.view.Proxy.node
    assert h.view.Proxy.coords.point.getValue()==[(0,0,-1.),(20,0,-1.),(20,10,-1.),(0,10,-1.)]
    assert not hasattr(h.obj,'Shape')


def test_constructor_explicitly_attaches_when_native_proxy_setter_has_already_run(native_paper):
    h=native_paper
    proxy=paper.ViewProviderPaper(h.view)
    assert proxy.view is h.view and h.view.DisplayMode=='Paper'
    assert h.view.SwitchNode.getNumChildren()==1


def test_native_automatic_first_attach_does_not_create_a_second_node(native_paper):
    h=native_paper;h.view.native_attached=False
    proxy=paper.ViewProviderPaper(h.view)
    assert proxy.view is h.view and h.view.SwitchNode.getNumChildren()==1


def test_repeat_restore_keeps_complete_proxy_node_and_hidden_user_styles(native_paper):
    h=native_paper;proxy=paper.ViewProviderPaper(h.view);node=proxy.node
    material=node.getChild(2);material.diffuseColor.setValue(.2,.3,.4)
    h.view.Visibility=False;h.view.DisplayMode='Paper'
    assignments=h.view.proxy_assignments
    assert paper.restore_document_paper(h.doc)==paper.restore_document_paper(h.doc)==1
    assert h.view.Proxy is proxy and h.view.proxy_assignments==assignments
    assert proxy.node is node and h.view.SwitchNode.getNumChildren()==1
    assert material.diffuseColor.getValue()==(.2,.3,.4) and h.view.Visibility is False
    assert h.view.DisplayMode=='Paper' and h.view.SwitchNode.whichChild.getValue()==-1


def test_incomplete_proxy_reuses_existing_authored_node_without_duplicate_or_style_reset(native_paper):
    h=native_paper;first=paper.ViewProviderPaper(h.view);node=first.node
    node.getChild(2).diffuseColor.setValue(.4,.5,.6)
    h.view.Proxy=paper.ViewProviderPaper.__new__(paper.ViewProviderPaper)
    assert paper.restore_document_paper(h.doc)==1
    assert h.view.Proxy is not first and h.view.Proxy.node is node
    assert h.view.SwitchNode.getNumChildren()==1
    assert h.view.Proxy.transform is node.getChild(0) and h.view.Proxy.coords is node.getChild(4)
    assert node.getChild(2).diffuseColor.getValue()==(.4,.5,.6)


def test_rebinding_reselects_native_paper_mask_even_when_enum_already_says_paper(native_paper):
    h=native_paper;first=paper.ViewProviderPaper(h.view);node=first.node
    h.view.Proxy=paper.ViewProviderPaper.__new__(paper.ViewProviderPaper)
    h.view.SwitchNode.whichChild.setValue(-1)
    assert h.view.DisplayMode=='Paper'
    paper.restore_document_paper(h.doc)
    assert h.view.DisplayMode=='Paper' and h.view.SwitchNode.getChild(h.view.SwitchNode.whichChild.getValue()) is node


def test_restoring_paper_does_not_replace_nonempty_custom_user_mode(native_paper):
    h=native_paper;paper.ViewProviderPaper(h.view)
    foreign=h.Node('Separator');h.view.addDisplayMode(foreign,'Custom')
    h.view.DisplayMode=['Paper','Custom'];h.view.DisplayMode='Custom'
    h.view.Proxy=paper.ViewProviderPaper.__new__(paper.ViewProviderPaper)
    paper.restore_document_paper(h.doc)
    assert h.view.DisplayMode=='Custom' and h.view.SwitchNode.getChild(h.view.SwitchNode.whichChild.getValue()) is foreign


@pytest.mark.parametrize('fault',['duplicate','malformed'])
def test_unknown_existing_paper_graph_is_preserved_and_refused(native_paper,fault):
    h=native_paper;proxy=paper.ViewProviderPaper(h.view);node=proxy.node
    if fault=='duplicate':h.view.SwitchNode.addChild(node)
    else:node.getChild(5).numVertices.setValues([3])
    h.view.Proxy=paper.ViewProviderPaper.__new__(paper.ViewProviderPaper)
    before=list(h.view.SwitchNode.children);mode=h.view.DisplayMode
    with pytest.raises(RuntimeError):paper.restore_document_paper(h.doc)
    assert h.view.SwitchNode.children==before and h.view.DisplayMode==mode


def test_complete_paper_retains_foreign_extra_display_node_and_properties(native_paper):
    h=native_paper;proxy=paper.ViewProviderPaper(h.view);node=proxy.node
    foreign=h.Node('DrawStyle');node.addChild(foreign)
    before=list(node.children)
    paper.restore_document_paper(h.doc)
    assert proxy.node is node and node.children==before
