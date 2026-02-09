"""
SSE Steel - Tekla PowerFab XML Parser
Parses FabSuiteDataExchange XML files into assembly and part records
"""
import xml.etree.ElementTree as ET
from typing import List, Dict, Any
import re
import math


def parse_tekla_xml(xml_content: str) -> Dict[str, Any]:
    """Parse a Tekla PowerFab XML export file and return structured data."""
    
    # Handle BOM
    if xml_content.startswith('\ufeff'):
        xml_content = xml_content[1:]
    
    root = ET.fromstring(xml_content)
    ns = {'fs': 'http://www.fabsuite.com/XML_Schemas/TeklaPowerFabDataFile0109.xsd'}
    
    result = {
        'project': {},
        'drawings': [],
        'assemblies': [],
        'summary': {}
    }
    
    # ─── PROJECT DATA ────────────────────────────────────
    project_data = root.find('.//fs:ProjectData', ns)
    if project_data is not None:
        contract = project_data.find('.//fs:ContractData', ns)
        if contract is not None:
            proj_id = contract.find('.//fs:ProjectId', ns)
            if proj_id is not None:
                result['project']['number'] = _text(proj_id, 'fs:ProjectNumber', ns)
                result['project']['name'] = _text(proj_id, 'fs:ProjectName', ns)
    
    # ─── DRAWINGS ────────────────────────────────────────
    drawing_data = root.find('.//fs:DrawingData', ns)
    if drawing_data is not None:
        for dwg in drawing_data.findall('fs:Drawing', ns):
            drawing = {
                'number': _text(dwg, 'fs:DrawingNumber', ns),
                'title': _text(dwg, 'fs:DrawingTitle', ns),
                'date_detailed': _text(dwg, 'fs:DateDetailed', ns),
                'category': _text(dwg, 'fs:Category', ns),
                'model_ref': _text(dwg, 'fs:ModelRef', ns),
            }
            rev = dwg.find('fs:DrawingRevision', ns)
            if rev is not None:
                drawing['revision_number'] = _text(rev, 'fs:RevisionNumber', ns)
                drawing['revision_description'] = _text(rev, 'fs:RevisionDescription', ns)
                drawing['date_revised'] = _text(rev, 'fs:DateRevised', ns)
            result['drawings'].append(drawing)
    
    # ─── ASSEMBLIES ──────────────────────────────────────
    assembly_data = root.find('.//fs:AssemblyData', ns)
    if assembly_data is not None:
        for asm in assembly_data.findall('fs:Assembly', ns):
            assembly = {
                'assembly_id': _text(asm, 'fs:AssemblyId', ns),
                'model_ref': _text(asm, 'fs:ModelRef', ns),
                'mark': _text(asm, 'fs:AssemblyMark', ns),
                'name': _text(asm, 'fs:AssemblyName', ns),
                'quantity': _int(asm, 'fs:AssemblyQuantity', ns),
                'drawing_number': _text(asm, 'fs:DrawingNumber', ns),
                'length_mm': _float_attr(asm, 'fs:AssemblyLength', ns),
                'finish_type': '',
                'parts': []
            }
            
            # Extract finish/coating - try multiple Tekla fields
            finish = _text(asm, 'fs:Coating', ns) or _text(asm, 'fs:FinishType', ns) or _text(asm, 'fs:AssemblyCoating', ns) or _text(asm, 'fs:SurfaceTreatment', ns)
            if not finish:
                # Try Remark for finish codes like ROP, HDG, etc.
                remark = _text(asm, 'fs:Remark', ns) or ''
                for code in ('ROP', 'HDG', 'GALV', 'PAINT', 'BARE', 'PRIME', 'SANDBLAST', 'NONE'):
                    if code in remark.upper():
                        finish = code
                        break
            assembly['finish_type'] = finish or ''
            
            # Sequence
            seq = asm.find('fs:AssemblySequence', ns)
            if seq is not None:
                assembly['sequence_number'] = _int(seq, 'fs:SequenceNumber', ns)
                assembly['sequence_lot_qty'] = _int(seq, 'fs:SequenceLotQuantity', ns)
            
            # Parts
            for part in asm.findall('fs:AssemblyPart', ns):
                part_data = {
                    'part_id': _text(part, 'fs:PartId', ns),
                    'model_ref': _text(part, 'fs:ModelRef', ns),
                    'part_mark': _text(part, 'fs:PartMark', ns),
                    'is_main_member': _text(part, 'fs:MainMember', ns) == 'true',
                    'quantity': _int(part, 'fs:PartQuantity', ns),
                    'shape': _text(part, 'fs:Shape', ns),
                    'dimensions': _text(part, 'fs:Dimensions', ns),
                    'grade': _text(part, 'fs:Grade', ns),
                    'length_raw': _text(part, 'fs:Length', ns),
                    'width_raw': _text(part, 'fs:Width', ns),
                    'remark': _text(part, 'fs:Remark', ns),
                    'pay_category': _text(part, 'fs:PayCategory', ns),
                }
                
                # Determine if hardware (bolts, nuts, washers, anchor bolts)
                hw_shapes = ('HS', 'NU', 'WA', 'AB', 'MB')
                part_data['is_hardware'] = part_data['shape'] in hw_shapes
                
                # Detect anchor bolts from shape or part mark
                shape_up = part_data['shape'].upper()
                mark_up = (part_data['part_mark'] or '').upper()
                dims_up = (part_data['dimensions'] or '').upper()
                part_data['is_anchor_bolt'] = (
                    shape_up == 'AB' or
                    'ANCHOR' in dims_up or
                    mark_up.startswith('AB') or
                    (shape_up == 'ROD' and 'ANCHOR' in (part_data.get('remark') or '').upper())
                )
                
                # Parse length to float
                try:
                    part_data['length_inches'] = float(part_data['length_raw']) if part_data['length_raw'] else 0
                except (ValueError, TypeError):
                    part_data['length_inches'] = 0
                
                # Parse width for plate parts
                try:
                    part_data['width_inches'] = float(part_data['width_raw']) if part_data.get('width_raw') else 0
                except (ValueError, TypeError):
                    part_data['width_inches'] = 0
                
                # Convert to display format (ft-in)
                part_data['length_display'] = inches_to_ft_in(part_data['length_inches'])
                
                assembly['parts'].append(part_data)
            
            # Calculate assembly weight from main member
            main_parts = [p for p in assembly['parts'] if p['is_main_member'] and not p['is_hardware']]
            assembly['main_member'] = main_parts[0] if main_parts else None
            
            result['assemblies'].append(assembly)
    
    # ─── SUMMARY ─────────────────────────────────────────
    unique_marks = set()
    total_assemblies = 0
    shape_counts = {}
    grade_counts = {}
    hardware_count = 0
    fabricated_count = 0
    
    for asm in result['assemblies']:
        unique_marks.add(asm['mark'])
        total_assemblies += asm['quantity']
        for part in asm['parts']:
            if part['is_hardware']:
                hardware_count += 1
            else:
                fabricated_count += 1
            shape = part['shape']
            grade = part['grade']
            shape_counts[shape] = shape_counts.get(shape, 0) + part['quantity']
            grade_counts[grade] = grade_counts.get(grade, 0) + part['quantity']
    
    result['summary'] = {
        'unique_marks': len(unique_marks),
        'total_assemblies': total_assemblies,
        'total_drawings': len(result['drawings']),
        'fabricated_parts': fabricated_count,
        'hardware_parts': hardware_count,
        'shape_breakdown': shape_counts,
        'grade_breakdown': grade_counts,
    }
    
    return result


def inches_to_ft_in(inches: float) -> str:
    """Convert decimal inches to feet-inches display format."""
    if not inches or inches == 0:
        return ""
    feet = int(inches // 12)
    remaining = inches % 12
    whole_in = int(remaining)
    frac = remaining - whole_in
    
    # Convert to nearest 1/16
    sixteenths = round(frac * 16)
    if sixteenths == 16:
        whole_in += 1
        sixteenths = 0
    if whole_in == 12:
        feet += 1
        whole_in = 0
    
    frac_str = ""
    if sixteenths > 0:
        # Simplify fraction
        num, den = sixteenths, 16
        for d in [8, 4, 2]:
            if num % (16 // d) == 0:
                num = num // (16 // d)
                den = d
                break
        frac_str = f"-{num}/{den}"
    
    if feet > 0:
        return f"{feet}'-{whole_in}{frac_str}\""
    else:
        return f"{whole_in}{frac_str}\""


def generate_qr_content(assembly_mark: str, job_number: str, part_id: int) -> str:
    """Generate QR code content string for a steel tag."""
    return f"SSE|{job_number}|{assembly_mark}|{part_id}"


def _text(element, tag, ns) -> str:
    """Safely get text from XML element."""
    el = element.find(tag, ns)
    return el.text.strip() if el is not None and el.text else ""

def _int(element, tag, ns) -> int:
    """Safely get integer from XML element."""
    val = _text(element, tag, ns)
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0

def _float_attr(element, tag, ns) -> float:
    """Safely get float from XML element text."""
    el = element.find(tag, ns)
    if el is not None and el.text:
        try:
            return float(el.text.strip())
        except (ValueError, TypeError):
            return 0.0
    return 0.0
