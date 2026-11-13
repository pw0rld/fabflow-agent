#!/usr/bin/env python3
"""Extract original STEP solid occurrences with native OCCT geometry.

No manufacturing decisions or mesh-to-solid reconstruction. Original bytes are
preserved; meshes/PNGs visualize the exact solid also exported to individual STEP.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from cad_thickness import infer_thickness
from cad_material import MaterialReader
from cad_laser import export_laser_cut
from cad_bends import recognize_bends
from cad_quote_quantities import measure_quote_quantities
from cad_step_subset import SourceStepSubset, SubsetError

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_SOLIDS = 200
MAX_FACES = 50000
MAX_TRIANGLES = 1000000
MAX_VERTICES = 2000000
MAX_JSON_BYTES = 48 * 1024 * 1024
MAX_DEPTH = 64
SCHEMA_VERSION = 1


class GeometryError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise GeometryError(message)


@contextmanager
def quiet_native():
    """Suppress OCCT transfer progress within this isolated worker process."""
    from OCP.Message import Message
    messenger=Message.DefaultMessenger_s()
    collection=messenger.ChangePrinters()
    printers=[collection.Value(i) for i in range(1,collection.Length()+1)]
    collection.Clear()
    sys.stdout.flush()
    old = os.dup(1)
    with open(os.devnull, 'wb') as null:
        try:
            os.dup2(null.fileno(), 1)
            yield
        finally:
            sys.stdout.flush()
            os.dup2(old, 1)
            os.close(old)
            for printer in printers:
                messenger.AddPrinter(printer)


def finite(value):
    value = float(value)
    require(math.isfinite(value) and abs(value) < 1e15, 'CAD 几何坐标或测量无效。')
    return round(value, 8)


def _imports():
    try:
        import OCP
        from OCP import TopAbs
        from OCP.Bnd import Bnd_Box
        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.BRepGProp import BRepGProp
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.BRepTools import BRepTools
        from OCP.IMeshTools import IMeshTools_Parameters, IMeshTools_MeshAlgoType_Delabella
        from OCP.GProp import GProp_GProps
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPCAFControl import STEPCAFControl_Reader
        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TCollection import TCollection_ExtendedString, TCollection_AsciiString
        from OCP.TDataStd import TDataStd_Name
        from OCP.TDF import TDF_Tool, TDF_Label
        from OCP.TDocStd import TDocStd_Document
        from OCP.TopExp import TopExp
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.collections import (Sequence_TDF_Label, Sequence_TCollection_AsciiString,
                                     IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as IndexedMap_TopoDS_Shape)
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise GeometryError('本机未配置原生 CAD 运行时，请配置 HYL_CAD_PYTHON（requirements-cad.txt）。') from exc
    return locals()


def _font(ImageFont, size):
    for name in ('/System/Library/Fonts/Supplemental/Arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def _thumbnail(item, path, deps):
    Image, ImageDraw, ImageFont = deps['Image'], deps['ImageDraw'], deps['ImageFont']
    width, height = 360, 300
    image = Image.new('RGB', (width, height), '#f4f7fa')
    draw = ImageDraw.Draw(image)
    points = item['mesh']['positions']
    vertices = [points[i:i + 3] for i in range(0, len(points), 3)]
    projected = [((x-y)/math.sqrt(2), (x+y-2*z)/math.sqrt(6), (x+y+z)/math.sqrt(3)) for x,y,z in vertices]
    xs, ys = [p[0] for p in projected], [p[1] for p in projected]
    min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
    scale = min(310 / max(max_x-min_x, 1e-8), 200 / max(max_y-min_y, 1e-8))
    center_x, center_y = (min_x+max_x)/2, (min_y+max_y)/2
    screen = [(width/2+(x-center_x)*scale, 130+(y-center_y)*scale) for x,y,_ in projected]
    ids = item['mesh']['indices']
    triangles = [ids[i:i+3] for i in range(0,len(ids),3)]
    triangles.sort(key=lambda t: sum(projected[i][2] for i in t))
    light = (0.35,-0.45,0.82)
    for tri in triangles:
        a,b,c = [vertices[i] for i in tri]
        u,v = [b[i]-a[i] for i in range(3)], [c[i]-a[i] for i in range(3)]
        normal = (u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0])
        length = math.sqrt(sum(x*x for x in normal))
        shade = .63+.32*abs(sum(normal[i]*light[i] for i in range(3))/max(length,1e-15))
        draw.polygon([screen[i] for i in tri], fill=tuple(round(channel*shade) for channel in (108,167,202)))
    draw.text((16,14),item['id'],fill='#24435d',font=_font(ImageFont,19))
    draw.text((16,240),item['name'].replace('\n',' ')[:45],fill='#24435d',font=_font(ImageFont,13))
    dims = ' x '.join(f'{size:.2f}' for size in item['bounds_mm']['size'])+' mm bbox'
    draw.text((16,261),dims,fill='#617287',font=_font(ImageFont,12))
    draw.text((16,278),'Native solid | isometric view',fill='#7a8fa0',font=_font(ImageFont,10))
    image.save(path)
    image.close()


def _contact_sheets(items, output, deps):
    Image,ImageDraw,ImageFont=deps['Image'],deps['ImageDraw'],deps['ImageFont']
    sheets=[]
    for start in range(0,len(items),8):
        rows=math.ceil(min(8,len(items)-start)/4)
        image=Image.new('RGB',(1440,rows*300+40),'white')
        draw=ImageDraw.Draw(image)
        draw.text((18,10),'Original CAD solid occurrences - geometry references, not approved manufacturing parts',
                  fill='#24435d',font=_font(ImageFont,17))
        for offset,item in enumerate(items[start:start+8]):
            with Image.open(output/item['thumbnail']) as thumb:
                image.paste(thumb,((offset%4)*360,40+(offset//4)*300))
        name=f'contact-{start//8+1:03d}.png'
        image.save(output/name)
        image.close()
        sheets.append(name)
    return sheets


def _solid_face_fingerprints(shape, deps, shape_map):
    faces=shape_map(shape,deps['TopAbs'].TopAbs_FACE)
    require(0<faces.Extent()<=512,'原 STEP 保真导出的逐面核验超出预算。')
    output=[]
    for index in range(1,faces.Extent()+1):
        face=deps['TopoDS'].Face(faces.FindKey(index))
        surface=deps['BRepAdaptor_Surface'](face,True)
        props=deps['GProp_GProps']()
        deps['BRepGProp'].SurfaceProperties_s(face,props,1e-9,False)
        center=props.CentreOfMass()
        box=deps['Bnd_Box']();deps['BRepBndLib'].AddOptimal_s(face,box,False,False)
        values=[props.Mass(),center.X(),center.Y(),center.Z(),box.GetXMin(),box.GetYMin(),box.GetZMin(),
                box.GetXMax(),box.GetYMax(),box.GetZMax()]
        require(all(math.isfinite(value) for value in values),'STEP 逐面几何核验数值无效。')
        output.append({'kind':str(surface.GetType()).split('.')[-1].removeprefix('GeomAbs_'),'values':values})
    return output


def _match_face_fingerprints(source, derived):
    """Conservative one-to-one multiset match; ambiguous near matches reject."""
    require(source and len(source)==len(derived) and len(source)<=512,'STEP 逐面几何数量不一致。')
    remaining=list(derived)
    for face in source:
        matches=[candidate for candidate in remaining if candidate['kind']==face['kind']
                 and len(candidate['values'])==len(face['values'])==10
                 and all(abs(a-b)<=1e-5 for a,b in zip(face['values'],candidate['values']))]
        require(matches,'STEP 单件逐面面积、重心或包络与原实体不一致。')
        # Exact duplicate faces can consume equivalent copies one at a time;
        # distinct near matches are not resolved by order or nearest distance.
        require(all(candidate['values']==matches[0]['values'] for candidate in matches),
                'STEP 单件逐面几何关联存在歧义。')
        remaining.remove(matches[0])
    require(not remaining,'STEP 单件存在未关联到原实体的面。')


def _verify_solid_step(exported, item, deps, shape_map, validation, *, strict=False, source_fingerprints=None):
    """Check a derivative against unchanged source scalars and world bounds."""
    rereader=deps['STEPControl_Reader']()
    with quiet_native():
        require(rereader.ReadFile(str(exported))==deps['IFSelect_RetDone'],'导出 STEP 无法重新读取。')
        if strict:
            units=[deps['Sequence_TCollection_AsciiString']() for _ in range(3)]
            rereader.FileUnits(*units)
            require(units[0].Length()==1 and units[0].Value(1).ToCString() in {'millimetre','millimeter','mm'},
                    '保真单件 STEP 的长度单位不明确或不受支持。')
        rereader.SetSystemLengthUnit(1.0)
        require(rereader.TransferRoots()>0,'导出 STEP 缺少几何。')
    exported_shape=rereader.OneShape()
    validation['solid_count']=shape_map(exported_shape,deps['TopAbs'].TopAbs_SOLID).Extent()
    require(validation['solid_count']==1,'导出 STEP 不是单一实体。')
    require(deps['BRepCheck_Analyzer'](exported_shape).IsValid(),'导出 STEP 拓扑无效。')
    shells=shape_map(exported_shape,deps['TopAbs'].TopAbs_SHELL)
    require(shells.Extent()>0 and all(deps['BRep_Tool'].IsClosed_s(shells.FindKey(i))
            for i in range(1,shells.Extent()+1)), '导出 STEP 未闭合。')
    if strict:
        source_topology=item.get('topology')
        require(isinstance(source_topology,dict) and type(source_topology.get('faces')) is int
                and isinstance(source_topology.get('surface_types'),dict),'原实体缺少可比较的面类型依据。')
        faces=shape_map(exported_shape,deps['TopAbs'].TopAbs_FACE)
        require(faces.Extent()==source_topology['faces'],'保真单件 STEP 的原始面数量不一致。')
        types=Counter(str(deps['BRepAdaptor_Surface'](deps['TopoDS'].Face(faces.FindKey(i)),True).GetType())
                      .split('.')[-1].removeprefix('GeomAbs_') for i in range(1,faces.Extent()+1))
        require(dict(types)==source_topology['surface_types'],'保真单件 STEP 的原始面类型不一致。')
    exported_volume=deps['GProp_GProps']()
    deps['BRepGProp'].VolumeProperties_s(exported_shape,exported_volume,True,False,False)
    relative=abs(abs(exported_volume.Mass())-item['volume_mm3'])/max(item['volume_mm3'],1e-12)
    require(math.isfinite(relative),'导出 STEP 的体积复测数值无效。')
    validation['volume_relative_delta']=relative
    box=deps['Bnd_Box']()
    deps['BRepBndLib'].AddOptimal_s(exported_shape,box,False,False)
    bounds=(box.GetXMin(),box.GetYMin(),box.GetZMin(),box.GetXMax(),box.GetYMax(),box.GetZMax())
    require(all(math.isfinite(value) for value in bounds),'导出 STEP 的包络复测数值无效。')
    validation['max_bound_delta_mm']=max(abs(a-b) for a,b in zip(bounds,item['bounds_mm']['min']+item['bounds_mm']['max']))
    require(relative<=1e-5 and validation['max_bound_delta_mm']<=1e-5,'导出 STEP 的几何偏差超出容许范围。')
    if strict:
        require(source_fingerprints is not None,'保真单件 STEP 缺少原实体逐面核验依据。')
        _match_face_fingerprints(source_fingerprints,_solid_face_fingerprints(exported_shape,deps,shape_map))


def _step_export_notice(exported, item, attribution, license_text):
    if not attribution:
        return
    notice='\n'.join(['Derived from Laser4DIY Enclosure; Copyright FabLab Muenchen e.V. 2021.',
                      'Source: '+attribution['source_url'],
                      'License: CERN-OHL-P v2. Modification: '+attribution['modification'],
                      'Modified at: '+attribution['modified_at']+'; solid '+item['id'],license_text])
    require('*/' not in notice,'公开样例许可文本无法写入 STEP 注释。')
    data=exported.read_bytes()
    require(b'HEADER;' in data,'CAD 单件缺少 STEP 文件头。')
    exported.write_bytes(data.replace(b'HEADER;',b'HEADER;\n/*\n'+notice.encode('utf-8')+b'\n*/',1))


def _export_solid_step(shape, item, output, deps, shape_map, *, attribution=None, license_text='', source_subset=None):
    """Keep original geometry readable when a derived STEP fails its own checks.

    Only an export/kernel rejection is local to this derivative. Filesystem
    failures still abort the worker rather than producing a misleading success.
    """
    exported=Path(output)/(item['id']+'.step')
    validation={'solid_count':None,'volume_relative_delta':None,'max_bound_delta_mm':None,
                'volume_relative_limit':1e-5,'max_bound_delta_limit_mm':1e-5}
    try:
        writer=deps['STEPControl_Writer']()
        with quiet_native():
            require(writer.Transfer(shape,deps['STEPControl_AsIs'])==deps['IFSelect_RetDone'],
                    'CAD 单件导出失败。')
            require(writer.Write(str(exported))==deps['IFSelect_RetDone'],'CAD 单件写入失败。')
        _step_export_notice(exported,item,attribution,license_text)
        # These checks apply only to the rewritten derivative. All measurements
        # and downstream laser/bend work continue to use the original shape.
        _verify_solid_step(exported,item,deps,shape_map,validation)
    except (GeometryError,RuntimeError) as error:
        # Only a completed one-solid rewrite whose measured geometry differs
        # may try original STEP bytes. Never rescue a bad/missing/multi-solid
        # derivative by changing tolerance or silently copying an assembly.
        if (source_subset is not None and validation['solid_count']==1
                and validation['volume_relative_delta'] is not None
                and validation['max_bound_delta_mm'] is not None
                and (validation['volume_relative_delta']>1e-5 or validation['max_bound_delta_mm']>1e-5)):
            try:
                exported.unlink(missing_ok=True)
                fingerprints=_solid_face_fingerprints(shape,deps,shape_map)
                proof=source_subset(exported,fingerprints)
                _step_export_notice(exported,item,attribution,license_text)
                recovered={'solid_count':None,'volume_relative_delta':None,'max_bound_delta_mm':None,
                           'volume_relative_limit':1e-5,'max_bound_delta_limit_mm':1e-5}
                _verify_solid_step(exported,item,deps,shape_map,recovered,strict=True,source_fingerprints=fingerprints)
                recovered.update(status='engineering_draft',extra_provenance=proof)
                item['step_file']=exported.name
                item['export_validation']=recovered
                return recovered
            except (GeometryError,SubsetError,RuntimeError,AttributeError,TypeError) as fallback_error:
                error=GeometryError(str(error)+'；原 STEP 保真导出未完成：'+str(fallback_error))
        # A rejected file must neither remain available nor be registered by the
        # service. In particular, this does not relax either geometric limit.
        exported.unlink(missing_ok=True)
        item['step_file']=None
        reason=' '.join(str(error).split())[:600] or '单件 STEP 导出未通过校验。'
        validation.update(status='rejected',reason=reason)
    else:
        item['step_file']=exported.name
        validation['status']='engineering_draft'
    item['export_validation']=validation
    return validation


def _tessellate(shape, faces, deps):
    """Mesh every original face; retry a different mesher without changing BRep.

    The original/default parameters remain the first attempt. A failed or
    incomplete attempt may leave caches attached to faces, so clear only these
    caches before one whole-solid Delabella attempt at the same deflection.
    The worker's process deadline remains the hard execution bound.
    """
    face_count=faces.Extent()
    require(0<face_count<=MAX_FACES,'CAD 实体面数量超出处理上限。')
    parameters={'linear_deflection_mm':0.15,'angular_deflection_rad':0.35,
                'relative':False,'in_parallel':False}
    attempts=[]

    def collect(mesher,algorithm):
        covered=[]
        missing=[]
        empty=[]
        for index in range(1,face_count+1):
            face=deps['TopoDS'].Face(faces.FindKey(index))
            location=deps['TopLoc_Location']()
            triangulation=deps['BRep_Tool'].Triangulation_s(face,location)
            face_id=f'F{index:04d}'
            if triangulation is None:
                missing.append(face_id)
            elif triangulation.NbNodes()<3 or triangulation.NbTriangles()<1:
                empty.append(face_id)
            else:
                covered.append((face,location,triangulation))
        attempt={'algorithm':algorithm,'is_done':bool(mesher.IsDone()),
                 'status_flags':int(mesher.GetStatusFlags()),
                 'covered_face_count':len(covered),'missing_face_ids':missing,
                 'empty_face_ids':empty}
        attempts.append(attempt)
        return covered,attempt

    default=deps['BRepMesh_IncrementalMesh'](shape,0.15,False,0.35,False)
    covered,attempt=collect(default,'default')
    fallback=not attempt['is_done'] or len(covered)!=face_count
    if fallback:
        recovery_deadline=time.monotonic()+20.0
        deps['BRepTools'].Clean_s(shape)
        settings=deps['IMeshTools_Parameters']()
        settings.Deflection=0.15
        settings.Angle=0.35
        settings.Relative=False
        settings.InParallel=False
        settings.AllowQualityDecrease=False
        settings.MeshAlgo=deps['IMeshTools_MeshAlgoType_Delabella']
        require(time.monotonic()<=recovery_deadline,'CAD 备选网格生成超时。')
        try:
            alternative=deps['BRepMesh_IncrementalMesh'](shape,settings)
            covered,attempt=collect(alternative,'delabella')
        except RuntimeError as error:
            raise GeometryError('CAD 备选网格生成失败：'+' '.join(str(error).split())[:300]) from error
        require(time.monotonic()<=recovery_deadline,'CAD 备选网格生成超时。')
    incomplete=attempt['missing_face_ids']+attempt['empty_face_ids']
    require(attempt['is_done'] and len(covered)==face_count,
            'CAD 网格生成未完整覆盖原始面；待处理面：'+
            (', '.join(incomplete[:32]) if incomplete else '网格任务未完成')+'。')

    positions,indices=[],[]
    for face,location,triangulation in covered:
        node_count=triangulation.NbNodes()
        triangle_count=triangulation.NbTriangles()
        require(len(positions)//3+node_count<=MAX_VERTICES and
                len(indices)//3+triangle_count<=MAX_TRIANGLES,
                'STEP 显示几何超过处理上限。')
        offset=len(positions)//3
        transform=location.Transformation()
        for point_index in range(1,node_count+1):
            point=triangulation.Node(point_index).Transformed(transform)
            positions.extend(finite(value) for value in (point.X(),point.Y(),point.Z()))
        reversed_face=face.Orientation()==deps['TopAbs'].TopAbs_REVERSED
        for triangle_index in range(1,triangle_count+1):
            a,b,c=triangulation.Triangle(triangle_index).Get()
            require(all(type(value) is int and 1<=value<=node_count for value in (a,b,c)),
                    'CAD 面三角索引无效。')
            if reversed_face:b,c=c,b
            indices.extend((offset+a-1,offset+b-1,offset+c-1))
    require(positions and indices,'CAD 实体没有可显示的面。')
    metadata={'status':'complete','algorithm':attempt['algorithm'],
              'fallback':fallback,'source_face_count':face_count,
              'covered_face_count':len(covered),'triangle_count':len(indices)//3,
              'parameters':parameters,'attempts':attempts}
    return {'positions':positions,'indices':indices},metadata


def extract_geometry(input_path, output_dir):
    source,output=Path(input_path),Path(output_dir)
    require(source.is_file() and not source.is_symlink(),'CAD 原件不存在或不可读取。')
    require(0<source.stat().st_size<=MAX_INPUT_BYTES,'STEP 原件为空或超过 20 MiB。')
    content=source.read_bytes()
    require(re.match(rb'\s*ISO-10303-21\s*;',content[:512].lstrip(b'\xef\xbb\xbf'),re.I),
            'CAD 原件不是可识别的 STEP 文件。')
    require(not output.is_symlink(),'CAD 输出目录无效。')
    output.mkdir(parents=True,mode=0o700,exist_ok=True)
    require(all(path.resolve()==source.resolve() for path in output.iterdir()),
            'CAD 输出目录只能包含本次输入原件，已有结果不会被覆盖。')
    d=_imports()
    T=d['TopAbs']
    reader=d['STEPCAFControl_Reader']()
    reader.SetNameMode(True)
    reader.SetColorMode(False)
    reader.SetMatMode(True)
    with quiet_native():
        require(reader.ReadFile(str(source))==d['IFSelect_RetDone'],'原生 CAD 内核无法打开 STEP 原件。')
    units=[d['Sequence_TCollection_AsciiString']() for _ in range(3)]
    reader.Reader().FileUnits(*units)
    names=[[seq.Value(i).ToCString() for i in range(1,seq.Length()+1)] for seq in units]
    require(len(names[0])==1,'STEP 长度单位缺失或不唯一，暂不生成制造件测量。')
    reader.Reader().SetSystemLengthUnit(1.0)
    document=d['TDocStd_Document'](d['TCollection_ExtendedString']('XCAF'))
    with quiet_native():
        require(reader.Transfer(document),'STEP 几何转换失败。')
    shape_tool=d['XCAFDoc_DocumentTool'].ShapeTool_s(document.Main())
    material_reader=MaterialReader(document,shape_tool)
    roots=d['Sequence_TDF_Label']()
    shape_tool.GetFreeShapes(roots)
    require(roots.Length()>0,'STEP 中没有可读取的形状。')
    solids,warnings,native_shapes=[],[],[]
    total_vertices=total_triangles=total_faces=0
    thickness_deadline=time.monotonic()+35.0

    def label_entry(label):
        text=d['TCollection_AsciiString']()
        d['TDF_Tool'].Entry_s(label,text)
        return text.ToCString()

    def label_name(label):
        attr=d['TDataStd_Name']()
        if label.FindAttribute(d['TDataStd_Name'].GetID_s(),attr):
            return attr.Get().ToExtString()[:512]
        return ''

    def shape_map(shape,kind):
        result=d['IndexedMap_TopoDS_Shape']()
        d['TopExp'].MapShapes_s(shape,kind,result)
        return result

    def add_solid(local_shape,definition,placement,occurrence,base_name,sub_index,count,
                  occurrence_label,inherited_labels):
        nonlocal total_vertices,total_triangles,total_faces
        require(len(solids)<MAX_SOLIDS,'STEP 实体超过本轮 200 件处理上限。')
        shape=d['TopoDS'].Solid(local_shape.Moved(placement))
        faces=shape_map(shape,T.TopAbs_FACE)
        shells=shape_map(shape,T.TopAbs_SHELL)
        closed=shells.Extent()>0 and all(d['BRep_Tool'].IsClosed_s(shells.FindKey(i)) for i in range(1,shells.Extent()+1))
        total_faces+=faces.Extent()
        require(total_faces<=MAX_FACES,'STEP 面数量超出处理上限。')
        box=d['Bnd_Box']()
        d['BRepBndLib'].AddOptimal_s(shape,box,False,False)
        require(not box.IsVoid() and not box.IsOpen(),'STEP 实体边界无效。')
        extents=(box.GetXMin(),box.GetYMin(),box.GetZMin(),box.GetXMax(),box.GetYMax(),box.GetZMax())
        minimum,maximum=[finite(v) for v in extents[:3]],[finite(v) for v in extents[3:]]
        area,volume=d['GProp_GProps'](),d['GProp_GProps']()
        d['BRepGProp'].SurfaceProperties_s(shape,area,False,False)
        d['BRepGProp'].VolumeProperties_s(shape,volume,True,False,False)
        valid=bool(d['BRepCheck_Analyzer'](shape).IsValid())
        surface_types=Counter()
        for index in range(1,faces.Extent()+1):
            kind=d['BRepAdaptor_Surface'](d['TopoDS'].Face(faces.FindKey(index)),True).GetType()
            surface_types[str(kind).split('.')[-1].removeprefix('GeomAbs_')]+=1
        geometry,mesh_generation=_tessellate(shape,faces,d)
        total_vertices+=len(geometry['positions'])//3
        total_triangles+=len(geometry['indices'])//3
        require(total_vertices<=MAX_VERTICES and total_triangles<=MAX_TRIANGLES,'STEP 显示几何超过处理上限。')
        solid_id=f'S{len(solids)+1:04d}'
        name=base_name or 'Unnamed CAD solid'
        if count>1:name+=f' / solid {sub_index}'
        item={'id':solid_id,'name':name[:512],
              'definition_id':label_entry(definition)+f'/solid-{sub_index}',
              'occurrence_path':occurrence,
              'topology':{'faces':faces.Extent(),'edges':shape_map(shape,T.TopAbs_EDGE).Extent(),
                          'vertices':shape_map(shape,T.TopAbs_VERTEX).Extent(),'valid':valid,
                          'closed':bool(closed),
                          'surface_types':dict(sorted(surface_types.items()))},
              'bounds_mm':{'min':minimum,'max':maximum,'size':[finite(maximum[i]-minimum[i]) for i in range(3)]},
              'area_mm2':finite(area.Mass()),'volume_mm3':finite(abs(volume.Mass())),
              'mesh':geometry,'mesh_generation':mesh_generation,
              'thumbnail':solid_id+'.png','step_file':solid_id+'.step'}
        if mesh_generation['fallback']:
            warnings.append(f'{solid_id} 使用备选网格算法完整读取原始面，原始几何及有效性状态未改变。')
        item['thickness']=infer_thickness(shape,faces,area_mm2=area.Mass(),
                                         volume_mm3=abs(volume.Mass()),deadline=thickness_deadline)
        item['material']=material_reader.read(definition,occurrence_label,local_shape,inherited_labels)
        if not valid:warnings.append(f'{solid_id} 原生几何校验未通过，测量及制造采用须人工复核。')
        solids.append(item)
        native_shapes.append(shape)

    def walk(label,placement,path,depth,ancestor_definitions,inherited_labels=()):
        require(depth<=MAX_DEPTH,'STEP 装配层级过深。')
        definition=label
        location=placement
        entry=label_entry(label)
        occurrence=path+[entry]
        if shape_tool.IsReference_s(label):
            definition=d['TDF_Label']()
            require(shape_tool.GetReferredShape_s(label,definition),'STEP 装配引用无效。')
            location=placement.Multiplied(shape_tool.GetLocation_s(label))
        definition_entry=label_entry(definition)
        require(definition_entry not in ancestor_definitions,'STEP 装配含循环引用。')
        if shape_tool.IsAssembly_s(definition):
            children=d['Sequence_TDF_Label']()
            shape_tool.GetComponents_s(definition,children)
            for index in range(1,children.Length()+1):
                walk(children.Value(index),location,occurrence,depth+1,ancestor_definitions|{definition_entry},
                     inherited_labels+(definition,label))
        else:
            shape=shape_tool.GetShape_s(definition)
            require(not shape.IsNull(),'STEP 装配引用空形状。')
            members=shape_map(shape,T.TopAbs_SOLID)
            if members.Extent()==0:warnings.append(f'原装配 {label_name(definition) or definition_entry} 没有封闭实体，未生成制造件候选。')
            for index in range(1,members.Extent()+1):
                add_solid(members.FindKey(index),definition,location,occurrence,label_name(definition),index,members.Extent(),
                          label,inherited_labels)

    for root_index in range(1,roots.Length()+1):
        walk(roots.Value(root_index),d['TopLoc_Location'](),[],0,set())
    require(solids,'STEP 未包含可处理的独立实体；曲面模型须工程补充。')
    result={'schema_version':SCHEMA_VERSION,'source_sha256':hashlib.sha256(content).hexdigest(),
            'engine':{'name':'OpenCascade','version':d['OCP'].__version__},
            'source_units':{'length_names':names[0],'angle_names':names[1],'solid_angle_names':names[2],
                            'conversion':'reader_system_length_unit','system_length_unit_mm':1.0},
            'output_unit':'mm','solids':solids,'warnings':warnings,'contact_sheets':[]}
    attribution=None
    license_text=''
    if result['source_sha256']=='57a5ecf18133054f7498211d9120e7d101f1e7a0afb7a86e6de42ef473fbe935':
        attribution={'title':'Laser4DIY Enclosure','author':'FabLab Muenchen e.V.',
                     'source_url':'https://github.com/Laser4DIY/Laser4DIY_Enclosure',
                     'license':'CERN-OHL-P-2.0','modified_at':datetime.now(timezone.utc).isoformat(),
                     'modification':'Individual original solid exported to STEP; PNG and mesh previews derived.'}
        result['source_attribution']=attribution
        license_path=Path(__file__).resolve().parents[1]/'samples/public-enclosure-20261026/LICENSE-CERN-OHL-P-v2.txt'
        license_text=license_path.read_text(encoding='utf-8')
    subset_exporter=None
    for item,shape in zip(solids,native_shapes):
        def preserve_source_subset(path, fingerprints, current=item):
            nonlocal subset_exporter
            if subset_exporter is None:
                subset_exporter=SourceStepSubset(source)
            require(subset_exporter.sha256==result['source_sha256'],'原始 STEP 保真导出来源不一致。')
            def verify(candidate):
                receipt={'solid_count':None,'volume_relative_delta':None,'max_bound_delta_mm':None}
                _verify_solid_step(candidate,current,d,shape_map,receipt,strict=True,source_fingerprints=fingerprints)
                return receipt
            _,provenance=subset_exporter.write_matching(path,verify)
            return provenance
        validation=_export_solid_step(shape,item,output,d,shape_map,
                                      attribution=attribution,license_text=license_text,source_subset=preserve_source_subset)
        if validation['status']=='rejected':
            warnings.append(f"{item['id']} 单件 STEP 导出未通过校验，已禁止下载；原始实体与测量保留。"
                            f"原因：{validation['reason']}")
        elif validation['volume_relative_delta']>1e-6:
            warnings.append(f"{item['id']} 的 STEP 重写复测体积偏差约 {validation['volume_relative_delta']*100:.6f}%，单件导出保持工程草稿待复核。")
        _thumbnail(item,output/item['thumbnail'],d)
    # Heavy flat-plate verification starts only after every original solid's
    # identity, material and thickness has been finalized on its own budget.
    from OCP.BRepTools import BRepTools
    for shape in native_shapes:
        try:
            BRepTools.Clean_s(shape)
        except RuntimeError:
            # Cache cleanup is an optimization, not a condition for preserving
            # the original solid. Individual laser verification can still fail.
            pass
    laser_deadline=time.monotonic()+45.0
    for item,shape in zip(solids,native_shapes):
        item['laser_cut']=export_laser_cut(shape,shape_map(shape,T.TopAbs_FACE),item['thickness'],
                                           item['id'],output,result['source_sha256'],volume_mm3=item['volume_mm3'],
                                           attribution=attribution,license_text=license_text,deadline=laser_deadline)
    bend_deadline=time.monotonic()+30.0
    for item,shape in zip(solids,native_shapes):
        item['bend_features']=recognize_bends(shape,shape_map(shape,T.TopAbs_FACE),item['thickness'],deadline=bend_deadline,
                                              laser_cut=item['laser_cut'])
    quote_deadline=time.monotonic()+12.0
    for item,shape in zip(solids,native_shapes):
        item['quote_quantities']=measure_quote_quantities(shape,item,result['source_sha256'],
            faces=shape_map(shape,T.TopAbs_FACE),deadline=quote_deadline)
    result['contact_sheets']=_contact_sheets(solids,output,d)
    encoded=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
    require(len(encoded)<=MAX_JSON_BYTES,'CAD 结构数据超出输出上限。')
    temporary=output/'.geometry.json.tmp'
    temporary.write_bytes(encoded)
    os.replace(temporary,output/'geometry.json')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=extract_geometry(args.input,args.output)
        import resource
        peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(json.dumps({'ok':True,'source_sha256':result['source_sha256'],'solid_count':len(result['solids']),
                          'output':str(args.output),'peak_rss_mib':round(peak/(1024**2 if sys.platform=='darwin' else 1024),1)},ensure_ascii=False))
        return 0
    except (GeometryError,OSError,RuntimeError,ValueError,TypeError) as error:
        print(json.dumps({'ok':False,'error':str(error)[:1500]},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
