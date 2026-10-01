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
