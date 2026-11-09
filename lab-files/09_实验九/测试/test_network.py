"""Mock-only LAN settings checks. No sockets, ROS calls or real ip command runs."""
import copy
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'工具'))
import check_network as netcheck

class NetworkTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(os.environ.get('LAB9_TEST_TMP',str(ROOT/'_工作记录'/'tests_tmp')))
        scratch.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=str(scratch))
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'settings.env'
        today=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        self.fields={'CHECK_DATE':today,'VM_IP':'192.168.50.10','ROBOT_IP':'192.168.50.20','MASTER_IP':'192.168.50.10'}
        self.interfaces=[{'ifname':'ens33','operstate':'UP','flags':['UP','BROADCAST','MULTICAST','LOWER_UP'],'addr_info':[{'local':'192.168.50.10','prefixlen':24}]}]
        self.routes=[{'dst':'192.168.50.20','dev':'ens33','prefsrc':'192.168.50.10'}]

    def validate(self,interfaces=None,routes=None,extra='',text=None):
        content='\n'.join('%s=%s'%x for x in self.fields.items())+'\n'+extra
        self.path.write_text(content if text is None else text,encoding='utf-8')
        replies=[json.dumps(self.interfaces if interfaces is None else interfaces),json.dumps(self.routes if routes is None else routes)]
        with mock.patch.object(netcheck.subprocess,'check_output',side_effect=replies) as read:
            result=netcheck.validate(self.path)
        self.assertEqual(read.call_count,2)
        self.assertEqual(read.call_args_list[0].args[0],['ip','-j','-4','addr','show'])
        self.assertEqual(read.call_args_list[1].args[0],['ip','-j','-4','route','get',self.fields['ROBOT_IP']])
        self.assertTrue(all(x.kwargs.get('timeout')==5 for x in read.call_args_list))
        return result

    def reject(self,**kwargs):
        with self.assertRaises((ValueError,subprocess.SubprocessError)):
            self.validate(**kwargs)

    def test_valid_direct_lan_and_master_a(self):
        self.assertEqual(self.validate(),self.fields)

    def test_master_b_allowed_without_assuming_a(self):
        self.fields['MASTER_IP']=self.fields['ROBOT_IP']
        self.assertEqual(self.validate()['MASTER_IP'],'192.168.50.20')

    def test_comments_blank_and_bom_allowed(self):
        text='\ufeff# onsite values\n\n'+'\n'.join('%s=%s'%x for x in self.fields.items())+'\n'
        self.assertEqual(self.validate(text=text),self.fields)

    def test_stale_date_rejected(self):
        self.fields['CHECK_DATE']='2000-01-01';self.reject()

    def test_duplicate_key_rejected(self):
        self.reject(extra='VM_IP=192.168.50.10\n')

    def test_unknown_key_rejected(self):
        self.reject(extra='PASSWORD=unused\n')

    def test_shell_expression_rejected_without_execution(self):
        self.fields['VM_IP']='$(touch /tmp/should-not-run)';self.reject()

    def test_missing_key_rejected(self):
        del self.fields['MASTER_IP'];self.reject()

    def test_public_robot_rejected(self):
        self.fields['ROBOT_IP']='8.8.8.8';self.reject()

    def test_same_endpoint_rejected(self):
        self.fields['ROBOT_IP']=self.fields['VM_IP'];self.reject()

    def test_third_master_rejected(self):
        self.fields['MASTER_IP']='192.168.50.30';self.reject()

    def test_gateway_route_rejected(self):
        r=copy.deepcopy(self.routes);r[0]['gateway']='192.168.50.1';self.reject(routes=r)

    def test_local_and_unreachable_routes_rejected(self):
        for value in ['local','broadcast','multicast','unreachable','blackhole','prohibit']:
            with self.subTest(route_type=value):
                r=copy.deepcopy(self.routes);r[0]['type']=value;self.reject(routes=r)

    def test_multiple_route_results_rejected(self):
        self.reject(routes=self.routes*2)

    def test_wrong_egress_source_rejected(self):
        r=copy.deepcopy(self.routes);r[0]['prefsrc']='192.168.50.11';self.reject(routes=r)

    def test_src_key_fallback_allowed(self):
        r=copy.deepcopy(self.routes);r[0]['src']=r[0].pop('prefsrc')
        self.assertEqual(self.validate(routes=r),self.fields)

    def test_wrong_egress_device_rejected(self):
        r=copy.deepcopy(self.routes);r[0]['dev']='eth9';self.reject(routes=r)

    def test_robot_outside_actual_prefix_rejected(self):
        i=copy.deepcopy(self.interfaces);i[0]['addr_info'][0]['prefixlen']=28;self.reject(interfaces=i)

    def test_network_and_broadcast_robot_addresses_rejected(self):
        for value in ['192.168.50.0','192.168.50.255']:
            with self.subTest(robot=value):
                self.fields['ROBOT_IP']=value;self.reject()

    def test_robot_address_on_this_vm_rejected(self):
        i=copy.deepcopy(self.interfaces);i.append({'ifname':'eth9','operstate':'UP','flags':['UP'],'addr_info':[{'local':self.fields['ROBOT_IP'],'prefixlen':24}]});self.reject(interfaces=i)

    def test_link_down_rejected(self):
        i=copy.deepcopy(self.interfaces);i[0]['operstate']='DOWN';self.reject(interfaces=i)

    def test_missing_admin_up_flag_rejected(self):
        i=copy.deepcopy(self.interfaces);i[0]['flags']=['BROADCAST'];self.reject(interfaces=i)

    def test_command_failure_is_bounded_and_propagates(self):
        self.path.write_text('\n'.join('%s=%s'%x for x in self.fields.items()),encoding='utf-8')
        with mock.patch.object(netcheck.subprocess,'check_output',side_effect=subprocess.TimeoutExpired('ip',5)) as read:
            with self.assertRaises(subprocess.TimeoutExpired):netcheck.validate(self.path)
        self.assertEqual(read.call_args.kwargs['timeout'],5)

if __name__=='__main__':unittest.main(verbosity=2)
