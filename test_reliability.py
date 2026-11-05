"""Regression tests for the existing queue; never start AEDT or write live Case data."""
from pathlib import Path
import contextlib
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
PRODUCTION=HERE.parents[1] if HERE.name=='candidate' and HERE.parent.name=='.repair_stage' else HERE
sys.path.insert(0,str(HERE))
# Staged imports use the real input/output paths only for read-only canaries.
if HERE!=PRODUCTION:
    config=types.ModuleType('settings');config.__file__=str(PRODUCTION/'settings.py')
    sys.modules['settings']=config
    exec(compile((HERE/'settings.py').read_text(encoding='utf-8'),config.__file__,'exec'),config.__dict__)
import settings
import publish
import scheduler
import native_io
from case_spec import case_by_id
from geometry import plan_case_geometry

class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def bundle(self):
        dest=self.base/'case_2183'
        shutil.copytree(settings.PUBLISHED/'case_2183',dest)
        return dest

    def test_all_real_complete_bundles(self):
        for cid in ('case_2183','case_2190','case_2191','case_2192','case_2193'):
            with self.subTest(cid=cid):
                self.assertTrue(publish.bundle_complete(settings.PUBLISHED/cid,cid))

    def test_validation_alone_is_not_complete(self):
        dest=self.base/'case_2183';dest.mkdir()
        shutil.copy2(settings.PUBLISHED/'case_2183'/'validation.json',dest/'validation.json')
        self.assertFalse(publish.bundle_complete(dest,'case_2183'))

    def test_missing_each_t0_is_not_complete(self):
        dest=self.bundle()
        for h in (250,500,1000,2000):
            f=dest/f'T0_h0_{h}_40x40_C.tab';original=f.read_bytes();f.unlink()
            self.assertFalse(publish.bundle_complete(dest,'case_2183'))
            f.write_bytes(original)

    def test_missing_ready_and_wrong_case_rejected(self):
        dest=self.bundle();(dest/'READY.json').unlink()
        self.assertFalse(publish.bundle_complete(dest,'case_2183'))
        self.assertFalse(publish.bundle_complete(settings.PUBLISHED/'case_2183','case_2191'))

    def test_tampered_field_shape_is_not_complete(self):
        dest=self.bundle()
        (dest/'T_source_top_128x128_area_C.tab').write_text('0 0 0 20\n',encoding='ascii')
        self.assertFalse(publish.bundle_complete(dest,'case_2183'))

    def test_nonfinite_physics_is_rejected(self):
        dest=self.bundle();p=dest/'validation.json';v=json.loads(p.read_text())
        v['source']['top']['mean_pixel_coverage']=float('nan')
        p.write_text(json.dumps(v))
        self.assertFalse(publish.bundle_complete(dest,'case_2183'))

    def test_cache_invalidates_on_missing_file(self):
        dest=self.bundle();self.assertTrue(publish.bundle_complete(dest,'case_2183'))
        (dest/'T0_h0_1000_40x40_C.tab').unlink()
        self.assertFalse(publish.bundle_complete(dest,'case_2183'))

    def test_corrupt_state_is_isolated_and_preserved(self):
        p=self.base/'case_0001.json';p.write_text('{broken')
        with patch.object(publish,'STATE',self.base):
            self.assertEqual(publish.read_state('case_0001')['status'],'STATE_CORRUPT')
        self.assertEqual(p.read_text(),'{broken')

    def test_owner_birth_rejects_recycled_pid(self):
        class Proc:
            def create_time(self):return 200.
            def cmdline(self):return ['python.exe',r'D:\3DIC_model\icepak_workflow\main.py','case','case_0001','--worker']
        with patch.object(publish.psutil,'Process',return_value=Proc()):
            self.assertFalse(publish.owner_alive({'case_id':'case_0001','owner_pid':10,'owner_birth':100.}))
            self.assertTrue(publish.owner_alive({'case_id':'case_0001','owner_pid':10,'owner_birth':200.}))

    def test_unaccepted_failed_workspace_is_not_deleted(self):
        with patch.object(publish,'read_state',return_value={'status':'FAILED'}), patch.object(publish.shutil,'rmtree') as deletion:
            self.assertFalse(publish.cleanup_case_work('case_0001',failed=True))
            deletion.assert_not_called()

    def test_state_preserves_native_identity_and_revision(self):
        with patch.object(publish,'STATE',self.base):
            publish.save_state('case_0001','MODELED',owner_pid=123,owner_birth=456,code_revision='r1',mesh_revision='m1')
            publish.save_state('case_0001','CHT_SOLVING')
            publish.save_state('case_0001','FAILED',reason='native failure')
            state=publish.read_state('case_0001')
            self.assertEqual((state['owner_pid'],state['owner_birth'],state['code_revision']),(123,456,'r1'))

    def test_request_file_refresh_and_append(self):
        p=self.base/'queue.txt'
        scheduler.append_requests(p,['case_5113'])
        self.assertEqual(scheduler.read_requests(p),['case_5113'])
        scheduler.append_requests(p,['case_5113','case_5114'])
        self.assertEqual(scheduler.read_requests(p),['case_5113','case_5114'])

    def test_physical_input_binding_rejects_wrong_geometry(self):
        from dataclasses import asdict
        dest=self.bundle();f=dest/'validation.json';proof=json.loads(f.read_text())
        proof['binding_contract']='FROZEN_PHYSICAL_INPUT_V1'
        proof['physical_input']=json.loads(json.dumps(asdict(case_by_id('case_2183'))))
        proof['code_revision']=settings.CODE_REVISION
        f.write_text(json.dumps(proof))
        (dest/'CHT_ADMISSION.json').write_text(json.dumps(publish.native_cht_admission(proof)))
        self.assertTrue(publish.bundle_complete(dest,'case_2183'))
        proof['physical_input']['sources'][0]['power_W']+=1
        f.write_text(json.dumps(proof))
        (dest/'CHT_ADMISSION.json').write_text(json.dumps(publish.native_cht_admission(proof)))
        self.assertFalse(publish.bundle_complete(dest,'case_2183'))

    def test_mesh_diagnostic_retained_before_long_solve(self):
        import icepak
        work=self.base/'case_0001';folder=work/'case.aedtresults';folder.mkdir(parents=True)
        mesh=folder/'current.nc_cas'
        mesh.write_bytes(b'(39 (7 solid wrong_zone_solid 1)(\n(3012 (7 1 4 1 4))\n')
        plan=types.SimpleNamespace(solids=[types.SimpleNamespace(name='active_top_source1',role='Source')])
        app=types.SimpleNamespace(odesktop=types.SimpleNamespace(GetProcessID=lambda:999999999))
        with self.assertRaises(native_io.NativeMeshError):
            icepak._native_mesh_audit(app,work,plan,'Setup1')
        report=json.loads((work/'NATIVE_MESH_AUDIT.json').read_text())
        self.assertEqual(report['stage'],'AFTER_MESH_BEFORE_FLOW')
        self.assertEqual(report['code'],'NATIVE_SOLID_CELL_MISSING')
        self.assertTrue(mesh.is_file())

    def test_native_orphan_solver_reserves_one_case_slot(self):
        class Child:
            def name(self):return 'fl_mpi2320.exe'
        class Parent:
            pid=901
            info={'name':'ansysedt.exe','cmdline':[str(scheduler.WORK/'case_0001'/'runtime_profile'/'Temp'/'x')]}
            def children(self,recursive=True):return [Child()]
        with patch.object(scheduler,'active_case_workers',return_value=()), patch.object(scheduler.psutil,'process_iter',return_value=[Parent()]):
            self.assertEqual(scheduler._foreign_workers({}),{901:'case_0001'})

    def test_hdm_mesh_only_stats_validate_native_cells(self):
        import icepak
        plan=types.SimpleNamespace(solids=[types.SimpleNamespace(name='active_top_source1',role='Source')])
        stats=self.base/'grid_stats_17'
        stats.write_text('# Name Cells Faces\nactive_top_source1 48 90\n')
        proof=icepak._hdm_source_occupancy([stats],plan)
        self.assertEqual(proof['source_cell_counts']['active_top_source1'],48)
        stats.write_text('# Name Cells Faces\nactive_top_source1 0 0\n')
        with self.assertRaises(native_io.NativeMeshError) as ctx:icepak._hdm_source_occupancy([stats],plan)
        self.assertEqual(ctx.exception.code,'NATIVE_SOLID_CELL_MISSING')
        stats.write_text('unrecognized mesh information')
        with self.assertRaises(native_io.NativeMeshError) as ctx:icepak._hdm_source_occupancy([stats],plan)
        self.assertEqual(ctx.exception.code,'NATIVE_MESH_STATS_UNSUPPORTED')

    def test_actual_hdm_missing_source_fixture_is_rejected(self):
        import icepak
        fixture=PRODUCTION/'tests'/'fixtures'/'hdm_source_rows_missing.txt'
        self.assertTrue(fixture.is_file(),'Retained native Source-row regression fixture missing')
        plan=plan_case_geometry(case_by_id('case_5116'))
        with self.assertRaises(native_io.NativeMeshError) as ctx:icepak._hdm_source_occupancy([fixture],plan)
        self.assertEqual(ctx.exception.code,'NATIVE_SOLID_CELL_MISSING')
        self.assertIn('active_top_source1',ctx.exception.diagnostic['missing'])

    def test_no_source_imprint_keeps_frozen_geometry_and_power(self):
        import geometry
        from unittest.mock import Mock
        plan=plan_case_geometry(case_by_id('case_5116'))
        made=[];imprints=[];sources=[]
        app=Mock()
        app.modeler.oeditor.GetObjectsInGroup.return_value=[]
        def create(origin,dimensions,*,name,material):
            made.append((name,origin,dimensions,material))
            return types.SimpleNamespace(name=name)
        app.modeler.create_box.side_effect=create
        app.modeler.imprint.side_effect=lambda blanks,tools,**kw:imprints.append((blanks,tools)) or True
        app.assign_source.side_effect=lambda *args,**kw:sources.append((args,kw)) or True
        result=geometry.create_native_cad(app,plan)
        self.assertEqual(result,[x.name for x in plan.solids])
        self.assertEqual(made,[(x.name,list(x.origin_mm),list(x.dimensions_mm),x.material) for x in plan.solids])
        self.assertFalse(any(any(name.startswith('active_') for name in tools) for blanks,tools in imprints))
        self.assertEqual(len(sources),len(plan.source_plan['sources']))

    def test_os_lock_blocks_duplicate_manager_and_releases(self):
        with patch.object(scheduler,'STATE',self.base):
            first=scheduler._queue_lock()
            try:
                with self.assertRaises(RuntimeError):scheduler._queue_lock()
            finally:scheduler._unlock(first)
            second=scheduler._queue_lock();scheduler._unlock(second)

    def test_duplicate_python_wrappers_count_once(self):
        p=types.SimpleNamespace(pid=1)
        with patch.object(scheduler,'active_case_workers',return_value=((1,'case_0001','new'),(2,'case_0001','new'),(3,'case_0002','new'),(4,'case_0002','new'))), patch.object(scheduler.psutil,'process_iter',return_value=[]):
            self.assertEqual(scheduler._foreign_workers({'case_0001':(p,None,0,0)}),{3:'case_0002'})

    def mesh(self,section='3012',zone='active_top_source1_solid'):
        p=self.base/'mesh.cas'
        p.write_bytes((f'(39 (7 solid {zone} 1)(\n({section} (7 1 4 1 4))\n').encode())
        plan=types.SimpleNamespace(solids=[types.SimpleNamespace(name='active_top_source1',role='Source')])
        return p,plan

    def test_cell_sections_12_2012_3012(self):
        for section in ('12','2012','3012'):
            p,plan=self.mesh(section)
            result=native_io.early_native_source_occupancy(p,plan)
            self.assertEqual(result['source_cell_counts']['active_top_source1_solid'],4)

    def test_unknown_format_not_reported_as_missing_source(self):
        p,plan=self.mesh('9999')
        with self.assertRaises(native_io.NativeMeshError) as ctx:native_io.early_native_source_occupancy(p,plan)
        self.assertEqual(ctx.exception.code,'NATIVE_CELL_FORMAT_UNSUPPORTED')

    def test_real_missing_source_is_still_rejected(self):
        p,plan=self.mesh(zone='passive_feol_solid')
        with self.assertRaises(native_io.NativeMeshError) as ctx:native_io.early_native_source_occupancy(p,plan)
        self.assertEqual(ctx.exception.code,'NATIVE_SOLID_CELL_MISSING')

    def test_native_pair_cannot_escape_workspace(self):
        mesh=self.base/'mesh';data=self.base/'data';mesh.write_bytes(b'x');data.write_bytes(b'x')
        work=self.base/'work';work.mkdir()
        with self.assertRaises(RuntimeError):native_io.bind_native_pair(work,mesh,data,'test')

    def test_native_pair_ignores_newer_unrelated_pair(self):
        work=self.base/'work';folder=work/'case.aedtresults';folder.mkdir(parents=True)
        mesh=folder/'current.nc_cas';data=folder/'current.fdat';mesh.write_bytes(b'm');data.write_bytes(b'd')
        native_io.bind_native_pair(work,mesh,data,'CHT_INITIAL')
        other=work/'unrelated.aedtresults';other.mkdir()
        (other/'current.nc_cas').write_bytes(b'wrong');(other/'current.fdat').write_bytes(b'wrong')
        self.assertEqual(native_io.find_native_pair(work),(mesh.resolve(),data.resolve()))

    def queue_trial(self,*,launch_bad=False,blocked=False,cleanup_error=False,append=False,code_change=False,drain=False):
        base=self.base
        for name in ('state','work','samples'):(base/name).mkdir(exist_ok=True)
        requests=base/'queue.txt';requests.write_text('case_0001\n'+('' if append else 'case_0002\n'))
        states={};launches=[];revision=[('initial',1,1)];ticks=[0]
        if blocked:
            (base/'work'/'case_0001').mkdir()
            states['case_0001']={'status':'FAILED'}
        class Proc:
            def __init__(self,cid):self.cid=cid;self.pid=100+len(launches)
            def poll(self):states[self.cid]={'status':'PUBLISHED'};return 0
        def popen(cmd,**kwargs):
            cid=cmd[cmd.index('case')+1]
            launches.append(cid)
            if launch_bad and cid=='case_0001':raise PermissionError('blocked launch')
            return Proc(cid)
        def sleep(_):
            ticks[0]+=1
            if append and ticks[0]==1:requests.write_text('case_0001\ncase_0002\n')
            if code_change and ticks[0]==1:revision[:]=[('changed',2,2)]
        def save(cid,status,**kw):states.setdefault(cid,{}).update(case_id=cid,status=status,**kw)
        class Me:
            def create_time(self):return 1.
        config={
            'STATE':base/'state','WORK':base/'work','SAMPLES':base/'samples','PAUSE_FILE':base/'pause',
            '_recover_orphaned_workers':lambda:0,'_foreign_workers':lambda active:{},
            '_admission_list':lambda ids:([x for x in ids if states.get(x,{}).get('status')!='PUBLISHED'],0),
            '_readiness':lambda policy,count=0:(True,{}),
            'legacy_reserved_status':lambda cid:'','has_accepted_transfer':lambda cid:False,
            'paired_interface_published':lambda cid:states.get(cid,{}).get('status')=='PUBLISHED',
            'live_native_for_case':lambda *a:False,'read_state':lambda cid:dict(states.get(cid,{})),
            'save_state':save,'production_signature':lambda:tuple(revision),
            '_progress':lambda cid,rec:{'stage':rec.get('status'),'cpu_seconds':0,'io_bytes':0,'state_updated':0},
        }
        if drain:(base/'pause').write_text('drain')
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(scheduler.__dict__,config))
            stack.enter_context(patch.object(scheduler.subprocess,'Popen',side_effect=popen))
            stack.enter_context(patch.object(scheduler.psutil,'Process',return_value=Me()))
            stack.enter_context(patch.object(scheduler.time,'sleep',side_effect=sleep))
            stack.enter_context(patch.object(scheduler,'cleanup_case_work',side_effect=PermissionError('locked log') if cleanup_error else lambda *a,**k:False))
            with contextlib.redirect_stdout(io.StringIO()):result=scheduler.run_queue(queue_file=requests,_max_cycles=5)
        return result,launches,states

    def test_launch_failure_does_not_block_next_case(self):
        result,launches,states=self.queue_trial(launch_bad=True)
        self.assertEqual(launches,['case_0001','case_0002']);self.assertEqual(result['passed'],1)

    def test_failed_workspace_does_not_spin_or_block_next(self):
        result,launches,states=self.queue_trial(blocked=True)
        self.assertEqual(launches,['case_0002']);self.assertEqual(result['passed'],1)

    def test_post_publish_cleanup_failure_does_not_stop_queue(self):
        result,launches,states=self.queue_trial(cleanup_error=True)
        self.assertEqual(result['passed'],2);self.assertEqual(launches,['case_0001','case_0002'])

    def test_new_request_is_seen_by_running_manager(self):
        result,launches,states=self.queue_trial(append=True)
        self.assertEqual(launches,['case_0001','case_0002']);self.assertEqual(result['passed'],2)

    def test_code_change_drains_and_does_not_launch_next(self):
        result,launches,states=self.queue_trial(code_change=True)
        self.assertEqual(result['status'],'DRAINED');self.assertEqual(launches,['case_0001'])

    def test_operator_drain_does_not_launch_any_case(self):
        result,launches,states=self.queue_trial(drain=True)
        self.assertEqual(result['status'],'DRAINED');self.assertEqual(launches,[])

    def test_single_invalid_input_is_isolated(self):
        master=self.base/'master.csv';master.write_text('case_id\ncase_0001\ncase_0002\n')
        cases={'case_0002':types.SimpleNamespace(eligible=True)};records={}
        with contextlib.ExitStack() as st:
            st.enter_context(patch.dict(scheduler.__dict__,{
                'CASE_MASTER':master,'load_cases':lambda:(cases,{'case_0001':'invalid geometry'}),
                'completed_rerun_ids':lambda:set(),'paired_interface_published':lambda cid:False,
                'has_accepted_transfer':lambda cid:False,'read_state':lambda cid:{},
                'owner_alive':lambda rec:False,'live_native_for_case':lambda *a:False,
                'save_state':lambda cid,status,**kw:records.update({cid:status})}))
            result,_=scheduler._admission_list(['case_0001','case_0002'])
        self.assertEqual(result,['case_0002']);self.assertEqual(records['case_0001'],'INPUT_INVALID')

if __name__=='__main__':unittest.main(verbosity=2)
