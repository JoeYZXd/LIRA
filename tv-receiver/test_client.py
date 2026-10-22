import unittest
from unittest.mock import patch
from client import Receiver

class Clock:
    now=0
    def monotonic(self):return self.now
    def sleep(self,n):self.now+=n

class SenderTests(unittest.TestCase):
    def test_requires_consent_before_network(self):
        receiver=Receiver({'host':'127.0.0.1','token':'test'})
        with patch.object(receiver,'request') as request:
            with self.assertRaises(ValueError):receiver.show({'text':'hello'})
            request.assert_not_called()

    def test_dismissal_never_launches_again(self):
        receiver=Receiver({'host':'127.0.0.1','token':'test'})
        def request(path,body=None):
            return {'interactive':True,'foreground':False,'rendered_id':'test-id'}
        with patch.object(receiver,'request',side_effect=request), patch('client.time',Clock()), patch('client.urllib.request.build_opener') as opener:
            with self.assertRaises(TimeoutError):receiver.show({'approved':True,'id':'test-id'})
            opener.assert_not_called()

    def test_offline_launch_retries_after_network_returns(self):
        receiver=Receiver({'host':'127.0.0.1','token':'test'});clock=Clock();launches=[]
        def launch(*args,**kwargs):
            launches.append(clock.now)
            raise OSError('Legacy endpoint never replies')
        def request(path,body=None):
            if len(launches)<2:raise OSError('Receiver not started')
            return {'interactive':True,'foreground':True,'rendered_id':'test-id'}
        with patch.object(receiver,'request',side_effect=request), patch('client.time',clock), patch('client.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=launch
            result=receiver.show({'approved':True,'id':'test-id'})
        self.assertEqual(result['rendered_id'],'test-id')
        self.assertEqual(launches,[0,10])


    def test_clear_supported_id_ack_and_no_launch(self):
        receiver=Receiver({'host':'127.0.0.1','token':'test'})
        responses=[{'interactive':True,'foreground':True,'capabilities':['clear_content_id']},{'cleared_id':'one'}]
        with patch.object(receiver,'request',side_effect=responses) as request, patch('client.urllib.request.build_opener') as opener:
            self.assertEqual(receiver.clear('one'),{'cleared_id':'one'})
            self.assertEqual(request.call_args_list[1].args,('/clear',{'expected_content_id':'one'}))
            opener.assert_not_called()

    def test_clear_old_off_and_dismissed_never_launch_or_post(self):
        receiver=Receiver({'host':'127.0.0.1','token':'test'})
        for status in ({'version':'0.1'},{'interactive':False,'foreground':True,'capabilities':['clear_content_id']},
                       {'interactive':True,'foreground':False,'capabilities':['clear_content_id']}):
            with self.subTest(status=status), patch.object(receiver,'request',return_value=status) as request, patch('client.urllib.request.build_opener') as opener:
                with self.assertRaises(RuntimeError):receiver.clear('one')
                self.assertEqual(request.call_count,1)
                opener.assert_not_called()

    def test_clear_mismatched_ack_is_not_success(self):
        receiver=Receiver({'host':'127.0.0.1','token':'test'})
        with patch.object(receiver,'request',side_effect=[{'interactive':True,'foreground':True,'capabilities':['clear_content_id']},{'cleared_id':'other'}]):
            with self.assertRaises(RuntimeError):receiver.clear('one')

if __name__=='__main__':unittest.main()
