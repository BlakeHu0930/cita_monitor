import copy
import unittest
from unittest.mock import Mock
from cita_monitor.browser_settings import launch_options, new_context


class BrowserSettingsTests(unittest.TestCase):
    def setUp(self):
        self.config={'browser':{'headless':True,'channel':'chromium','normalize_headless_user_agent':True}}

    def test_new_headless_launch_and_visible_inspection_override(self):
        self.assertEqual(launch_options(self.config),{'headless':True,'channel':'chromium'})
        self.assertEqual(launch_options(self.config,headless=False),{'headless':False,'channel':'chromium'})

    def test_ua_uses_real_browser_version_and_preserves_platform(self):
        b=Mock();temp=Mock();context=Mock();b.new_context.side_effect=[temp,context]
        ua='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 HeadlessChrome/148.0.7777.1 Safari/537.36'
        temp.new_page.return_value.evaluate.return_value=ua
        self.assertIs(new_context(b,self.config),context)
        temp.close.assert_called_once()
        b.new_context.assert_called_with(locale='es-ES',user_agent=ua.replace('HeadlessChrome/','Chrome/'))
        context.add_init_script.assert_not_called()

    def test_visible_context_does_not_change_user_agent(self):
        b=Mock();new_context(b,self.config,headless=False)
        b.new_context.assert_called_once_with(locale='es-ES')

    def test_normalization_can_be_disabled(self):
        c=copy.deepcopy(self.config);c['browser']['normalize_headless_user_agent']=False
        b=Mock();new_context(b,c)
        b.new_context.assert_called_once_with(locale='es-ES')

    def test_default_config_preserves_previous_launch_options(self):
        c={'browser':{'headless':False}}
        self.assertEqual(launch_options(c),{'headless':False})

    def test_temporary_context_closes_on_evaluation_failure(self):
        b=Mock();b.new_context.return_value.new_page.return_value.evaluate.side_effect=RuntimeError('failed')
        with self.assertRaises(RuntimeError):new_context(b,self.config)
        b.new_context.return_value.close.assert_called_once()


if __name__=='__main__':unittest.main()
