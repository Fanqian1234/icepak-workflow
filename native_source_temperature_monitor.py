"""Configure a read-only native cell-temperature monitor, without a case whitelist."""
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import re

SOURCE = Path(__file__).with_suffix('.c')
LIBRARY = 'libnci_temperature'


def prepare(mesh, work, monitored_names):
    """Bind explicit actual solid-zone IDs, preserving the existing zone selection."""
    if not os.environ.get('PROCESSOR_ARCHITECTURE'):
        architecture=platform.machine().upper()
        if architecture!='AMD64':raise RuntimeError('Unverified native compiler architecture')
        os.environ['PROCESSOR_ARCHITECTURE']=architecture
    with Path(mesh).open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        # Fluent persists loaded libraries in the RP header of its legacy case.
        # Recompiling a still-loaded DLL is invalid on Windows.
        loaded=re.search(rb'\(udf/libname \(([^\n]*)\)\)',raw[:262144])
        reload_library=bool(loaded and ('"'+LIBRARY+'"').encode('ascii') in loaded[1])
        names = {}
        for match in re.finditer(rb'(?m)^\(39 \((\d+) solid (\S+) 1\)\(', raw):
            names.setdefault(match[2].decode('ascii'), []).append(int(match[1]))
    ids = {}
    for layer in ('bottom', 'top'):
        selected = monitored_names[layer]
        if not selected or any(len(names.get(name, [])) != 1 for name in selected):
            raise ValueError('Native source-temperature zone selection is missing or ambiguous')
        ids[layer] = [names[name][0] for name in selected]
    if set(ids['bottom']) & set(ids['top']):
        raise ValueError('Bottom and top monitor zones overlap')
    source = SOURCE.read_bytes()
    binding = hashlib.sha256(source + json.dumps(ids, sort_keys=True).encode('ascii')).hexdigest()
    header = '\n'.join([
        '#define NCI_SOURCE_BINDING "' + binding + '"',
        *['#define NCI_' + layer.upper() + '_COUNT ' + str(len(ids[layer])) + '\n'
          + 'static const int nci_' + layer + '_ids[] = {' + ','.join(map(str, ids[layer])) + '};'
          for layer in ('bottom', 'top')], ''])
    directory = Path(work) / 'tmp'; directory.mkdir(exist_ok=True)
    (directory / SOURCE.name).write_bytes(source)
    (directory / 'nci_source_temperature_ids.h').write_text(header, encoding='ascii')
    commands = ['/define/user-defined/use-built-in-compiler?', 'yes',
        '/define/user-defined/compiled-functions', 'compile', '"' + LIBRARY + '"', 'yes',
        '"' + SOURCE.name + '"', '""', '"nci_source_temperature_ids.h"', '""',
        '/define/user-defined/compiled-functions', 'load', '"' + LIBRARY + '"']
    if reload_library:
        commands=['/define/user-defined/compiled-functions','unload','"'+LIBRARY+'"',*commands]
    report = ['/define/user-defined/execute-on-demand', '"nci_source_temperatures::' + LIBRARY + '"']
    return dict(binding=binding, source_sha256=hashlib.sha256(source).hexdigest(),
                compiler_environment={'PROCESSOR_ARCHITECTURE':os.environ['PROCESSOR_ARCHITECTURE']},
                zone_ids=ids, monitored_names=monitored_names, compile_commands=commands, report_commands=report)


def iteration_reports(commands, prepared):
    """Run read-only reductions between ten-step blocks, at the TUI top level."""
    commands=list(commands)
    callback=[i for i,line in enumerate(commands)
              if line.startswith('/solve/execute-commands/add-edit nci_thermal_monitor ')]
    if len(callback)!=1:raise ValueError('Expected one original temperature callback')
    del commands[callback[0]]
    begin='(format #t "THERMAL_MONITOR_BEGIN~%")'
    end='(format #t "THERMAL_MONITOR_END~%")'
    if commands.count(begin)!=1 or commands.count(end)!=1:
        raise ValueError('Expected one final legacy temperature report')
    first,last=commands.index(begin),commands.index(end)
    if first>=last:raise ValueError('Invalid temperature report boundary')
    del commands[first:last+1]
    if commands.count('/solve/iterate')!=1:raise ValueError('Expected one bounded iteration request')
    index=commands.index('/solve/iterate');steps=int(commands[index+1])
    if not 0<steps<=450:raise ValueError('Invalid iteration count')
    blocks=[]
    while steps:
        count=min(10,steps)
        blocks+=['/solve/iterate',str(count),*prepared['report_commands']]
        steps-=count
    commands[index:index+2]=blocks
    if not commands[0].startswith('/file/read-case ') or not commands[1].startswith('/file/read-data '):
        raise ValueError('Missing explicit native input pair')
    commands[2:2]=prepared['compile_commands']
    return commands


def validate_reports(log, prepared):
    bindings=re.findall(r'^NCI_SOURCE_MONITOR_BINDING=(\w+)$',log,re.M)
    blocks=log.count('\nTHERMAL_MONITOR_END\n')
    if not bindings or len(bindings)!=blocks or any(value!=prepared['binding'] for value in bindings):
        raise ValueError('Native temperature report identity is incomplete or different')
    cells=re.findall(r'^NCI_SOURCE_MONITOR_CELLS bottom=(\d+) top=(\d+) volume_bottom=([\d.eE+-]+) volume_top=([\d.eE+-]+)$',log,re.M)
    if len(cells)!=blocks or any(not all(float(v)>0 for v in row) for row in cells):
        raise ValueError('Native temperature report has missing cells or volume')
    return blocks


def _canonical_report_payloads(log):
    """Compare native cell reports, not unrelated Fluent prologue/trailer formatting.

    Fluent and Windows text round-trips may change line breaks outside the
    measured report. Preserve every binding, cell count and numeric value.
    """
    pattern=r'(?ms)^NCI_SOURCE_MONITOR_BINDING=[0-9a-f]+\s*\n.*?^THERMAL_MONITOR_END\s*$'
    return [re.sub(r'\s+',' ',match.group()).strip()
            for match in re.finditer(pattern,log)]


def report_evidence(work, log):
    """Bind compiled-report sources, real native payloads and native transcripts."""
    from collections import Counter
    from queue_runtime import read,sha
    bindings=re.findall(r'^NCI_SOURCE_MONITOR_BINDING=(\w+)$',log,re.M)
    if not bindings:return None
    merged=Counter(_canonical_report_payloads(log))
    if sum(merged.values())!=len(bindings):
        raise ValueError('Assembled native temperature payload count differs from its bindings')
    work=Path(work);source=work/'tmp'/SOURCE.name
    source_hash=sha(source)
    if source_hash!=sha(SOURCE):raise ValueError('Compiled monitor source differs from the validated implementation')
    files={str(source):source_hash};observed=[];proved=Counter()
    candidates=list(work.glob('*_temperature_monitor.json'))+list(work.glob('REPORT_*_RESIDENT.json'))
    for path in candidates:
        row=read(path)
        if row.get('status')=='VERIFIED_NATIVE_RESIDENT_CELL_REPORTS':
            prepared=row['prepared'];native_log=Path(row.get('native_log',work/(row['mode']+'.log')))
        elif row.get('status')=='NATIVE_FLOW_AND_TOP_LEVEL_REPORT_COMPLETED':
            prepared=row['resident_temperature_monitor'];native_log=path.with_name(path.stem.lower()+'.log')
        else:continue
        if prepared['binding'] not in bindings:continue
        expected=hashlib.sha256(source.read_bytes()+json.dumps(prepared['zone_ids'],sort_keys=True).encode('ascii')).hexdigest()
        if expected!=prepared['binding'] or prepared['source_sha256']!=source_hash:
            raise ValueError('Native report source or actual zone-ID binding changed')
        native_text=native_log.read_text(errors='replace')
        payloads=Counter(_canonical_report_payloads(native_text))
        if not payloads or payloads-merged:continue
        count=validate_reports(native_text,prepared)
        if sum(payloads.values())!=count or proved+payloads-merged:
            raise ValueError('Native temperature evidence is duplicated or incomplete')
        proved.update(payloads)
        observed.extend([prepared['binding']]*count)
        journal=Path(row.get('native_journal',native_log.with_suffix('.jou')))
        for evidence in (path,native_log,journal):files[str(evidence)]=sha(evidence)
    if sorted(observed)!=sorted(bindings) or proved!=merged:
        raise ValueError('Missing native source-temperature execution evidence')
    return dict(method='RESIDENT_CELL_REDUCTION_V1',source_sha256=source_hash,
        native_report_bindings=bindings,files_sha256=files)

