# -*- coding: utf-8 -*-

try:
    from unittest import mock
except ImportError:
    import mock

from django.conf import settings
from django.shortcuts import resolve_url
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse, reverse_lazy
from django.utils import six
from django_otp.oath import totp
from django_otp.util import random_hex

from two_factor.models import PhoneDevice
from two_factor.utils import backup_phones
from two_factor.validators import validate_international_phonenumber
from two_factor.views.core import PhoneDeleteView, PhoneSetupView

from .utils import UserMixin


@override_settings(
    TWO_FACTOR_SMS_GATEWAY='two_factor.gateways.fake.Fake',
    TWO_FACTOR_CALL_GATEWAY='two_factor.gateways.fake.Fake',
)
class PhoneSetupTest(UserMixin, TestCase):
    def setUp(self):
        super(PhoneSetupTest, self).setUp()
        self.user = self.create_user()
        self.enable_otp()
        self.login_user()

    def test_form(self):
        response = self.client.get(reverse('two_factor:phone_create'))
        self.assertContains(response, 'Method:')

    def _post(self, data=None):
        return self.client.post(reverse('two_factor:phone_create'), data=data)

    @mock.patch('two_factor.gateways.fake.Fake')
    def test_setup(self, fake):
        response = self._post({'phone_setup_view-current_step': 'setup',
                               'setup-method': ''})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'method': ['This field is required.']})

        response = self._post({'phone_setup_view-current_step': 'setup',
                               'setup-method': 'call'})

        self.assertContains(response, 'called on')

        response = self._post({'phone_setup_view-current_step': 'call',
                               'call-number': '+31101234567',
                               'call-extension': ''})
        self.assertContains(response, 'We\'ve sent a token to your phone')
        device = response.context_data['wizard']['form'].device

        fake.return_value.make_call.assert_called_with(
            device=device, token='%06d' % totp(device.bin_key))

        response = self._post({'phone_setup_view-current_step': 'validation',
                               'validation-token': '123456'})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'token': ['Entered token is not valid.']})

        response = self._post({'phone_setup_view-current_step': 'validation',
                               'validation-token': totp(device.bin_key)})
        self.assertRedirects(response, resolve_url(settings.LOGIN_REDIRECT_URL))
        phones = self.user.phonedevice_set.all()
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0].name, 'backup')
        self.assertEqual(phones[0].number.as_e164, '+31101234567')
        self.assertEqual(phones[0].key, device.key)

    @mock.patch('two_factor.gateways.fake.Fake')
    @override_settings(TWO_FACTOR_EXTENSION=False)
    def test_setup_phone_ext_disabled(self, fake):
        self._post(data={'phone_setup_view-current_step': 'setup',
                         'setup-method': 'call'})

        response = self._post(data={'phone_setup_view-current_step': 'call',
                                    'call-number': '+31101234567',
                                    'call-extension': '0400'})

        self.assertContains(response, 'We\'ve sent a token to your phone')

        # assert that the token was send to the gateway
        self.assertEqual(
            fake.return_value.method_calls,
            [mock.call.make_call(device=mock.ANY, token=mock.ANY)]
        )

        response = self._post(data={'phone_setup_view-current_step': 'validation',
                                    'validation-token': '666'})
        self.assertEqual(response.context_data['wizard']['form'].errors,
                         {'token': ['Entered token is not valid.']})

        # submitting correct token should finish the setup
        token = fake.return_value.make_call.call_args[1]['token']
        response = self._post(data={'phone_setup_view-current_step': 'validation',
                                    'validation-token': token})
        self.assertRedirects(response, resolve_url(settings.LOGIN_REDIRECT_URL))

        phones = self.user.phonedevice_set.all()
        self.assertEqual(len(phones), 1)
        self.assertEqual(phones[0].name, 'backup')
        self.assertEqual(phones[0].number.as_e164, '+31101234567')
        # extension should not be populated
        self.assertFalse(phones[0].extension)
        self.assertEqual(phones[0].method, 'call')

    @mock.patch('two_factor.gateways.fake.Fake')
    def test_number_validation(self, fake):
        self._post({'phone_setup_view-current_step': 'setup',
                    'setup-method': 'sms'})
        response = self._post({'phone_setup_view-current_step': 'sms',
                               'sms-number': '123'})
        self.assertEqual(
            response.context_data['wizard']['form'].errors,
            {'number': [six.text_type(validate_international_phonenumber.message)]})

    @mock.patch('formtools.wizard.views.WizardView.get_context_data')
    def test_success_url_as_url(self, get_context_data):
        url = '/account/two_factor/'
        view = PhoneSetupView()
        view.success_url = url

        def return_kwargs(form, **kwargs):
            return kwargs
        get_context_data.side_effect = return_kwargs

        context = view.get_context_data(None)
        self.assertIn('cancel_url', context)
        self.assertEqual(url, context['cancel_url'])

    @mock.patch('formtools.wizard.views.WizardView.get_context_data')
    def test_success_url_as_named_url(self, get_context_data):
        url_name = 'two_factor:profile'
        url = reverse(url_name)
        view = PhoneSetupView()
        view.success_url = url_name

        def return_kwargs(form, **kwargs):
            return kwargs
        get_context_data.side_effect = return_kwargs

        context = view.get_context_data(None)
        self.assertIn('cancel_url', context)
        self.assertEqual(url, context['cancel_url'])

    @mock.patch('formtools.wizard.views.WizardView.get_context_data')
    def test_success_url_as_reverse_lazy(self, get_context_data):
        url_name = 'two_factor:profile'
        url = reverse(url_name)
        view = PhoneSetupView()
        view.success_url = reverse_lazy(url_name)

        def return_kwargs(form, **kwargs):
            return kwargs
        get_context_data.side_effect = return_kwargs

        context = view.get_context_data(None)
        self.assertIn('cancel_url', context)
        self.assertEqual(url, context['cancel_url'])


class PhoneDeleteTest(UserMixin, TestCase):
    def setUp(self):
        super(PhoneDeleteTest, self).setUp()
        self.user = self.create_user()
        self.backup = self.user.phonedevice_set.create(name='backup', method='sms', number='+1')
        self.default = self.user.phonedevice_set.create(name='default', method='call', number='+1')
        self.login_user()

    def test_delete(self):
        response = self.client.post(reverse('two_factor:phone_delete',
                                            args=[self.backup.pk]))
        self.assertRedirects(response, resolve_url(settings.LOGIN_REDIRECT_URL))
        self.assertEqual(list(backup_phones(self.user)), [])

    def test_cannot_delete_default(self):
        response = self.client.post(reverse('two_factor:phone_delete',
                                            args=[self.default.pk]))
        self.assertContains(response, 'was not found', status_code=404)

    def test_success_url_as_url(self):
        url = '/account/two_factor/'
        view = PhoneDeleteView()
        view.success_url = url
        self.assertEqual(view.get_success_url(), url)

    def test_success_url_as_named_url(self):
        url_name = 'two_factor:profile'
        url = reverse(url_name)
        view = PhoneDeleteView()
        view.success_url = url_name
        self.assertEqual(view.get_success_url(), url)

    def test_success_url_as_reverse_lazy(self):
        url_name = 'two_factor:profile'
        url = reverse(url_name)
        view = PhoneDeleteView()
        view.success_url = reverse_lazy(url_name)
        self.assertEqual(view.get_success_url(), url)


class PhoneDeviceTest(UserMixin, TestCase):
    def test_verify(self):
        for no_digits in (6, 8):
            with self.settings(TWO_FACTOR_TOTP_DIGITS=no_digits):
                device = PhoneDevice(key=random_hex().decode())
                self.assertFalse(device.verify_token(-1))
                self.assertFalse(device.verify_token('foobar'))
                self.assertTrue(device.verify_token(totp(device.bin_key, digits=no_digits)))

    def test_verify_token_as_string(self):
        """
        The field used to read the token may be a CharField,
        so the PhoneDevice must be able to validate tokens
        read as strings
        """
        for no_digits in (6, 8):
            with self.settings(TWO_FACTOR_TOTP_DIGITS=no_digits):
                device = PhoneDevice(key=random_hex().decode())
                self.assertTrue(device.verify_token(str(totp(device.bin_key, digits=no_digits))))

    def test_unicode(self):
        device = PhoneDevice(name='unknown')
        self.assertEqual('unknown (None)', str(device))

        device.user = self.create_user()
        self.assertEqual('unknown (bouke@example.com)', str(device))
