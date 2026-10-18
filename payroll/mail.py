"""SMTP backend that connects the way Python's own smtplib does.

Django's default backend greets the mail server with this computer's name. Some servers, Gmail included,
drop the connection when that name looks odd, as many Windows computer names do. Here smtplib picks the
greeting itself, which is what plain Python scripts use.
"""
import smtplib

from django.core.mail.backends.smtp import EmailBackend


class PayrollSMTPBackend(EmailBackend):
    def open(self):
        if self.connection:
            return False
        try:
            if self.use_ssl:
                self.connection = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                                   context=self.ssl_context)
            else:
                self.connection = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                self.connection.ehlo()
                if self.use_tls:
                    self.connection.starttls(context=self.ssl_context)
                    self.connection.ehlo()
            if self.username and self.password:
                # smtplib retries with a second login method after a rejection, and Gmail then hangs up,
                # which hides the real error. Offering PLAIN only keeps the real message.
                features = self.connection.esmtp_features
                if "PLAIN" in features.get("auth", "").split():
                    features["auth"] = "PLAIN"
                self.connection.login(self.username, self.password)
            return True
        except OSError:                       # smtplib's own errors are OSErrors too
            self._discard()
            if not self.fail_silently:
                raise

    def _discard(self):
        try:
            if self.connection:
                self.connection.close()
        except OSError:
            pass
        self.connection = None
