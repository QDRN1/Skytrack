"""Alert system: humidity threshold alerts via SMTP email."""

import smtplib
import logging
from email.mime.text import MIMEText
from datetime import datetime

logger = logging.getLogger('skytrack.alerts')


class AlertService:
    def __init__(self, config):
        self.config = config
        self.last_alert_time = None
        self._cooldown = config.get('alert_cooldown', 300)

    def trigger_humidity_alert(self, sensor_data):
        """Send an alert if humidity exceeds threshold. Respects cooldown."""
        now = datetime.now()

        if self.last_alert_time:
            elapsed = (now - self.last_alert_time).total_seconds()
            if elapsed < self._cooldown:
                logger.debug(f'Alert cooldown active ({int(self._cooldown - elapsed)}s remaining)')
                return False

        humidity = sensor_data.get('humidity', 0)
        threshold = self.config.get('humidity_threshold', 80)

        logger.warning(f'HUMIDITY ALERT: {humidity}% exceeds threshold {threshold}%')
        self.last_alert_time = now

        if self.config.get('smtp_host'):
            self._send_email(
                subject=f"[SkyTrack] Humidity Alert: {humidity}%",
                body=(
                    f"Humidity reading of {humidity}% exceeds threshold of {threshold}%.\n"
                    f"Temperature: {sensor_data.get('temperature_f', 'N/A')}\u00b0F\n"
                    f"Time: {now.isoformat()}\n"
                    f"Device: {self.config.get('device_name', 'QDRNow-SkyTrack')}"
                ),
            )
        return True

    def _send_email(self, subject, body):
        try:
            msg = MIMEText(body)
            msg['Subject'] = subject
            msg['From'] = self.config.get('alert_from', '')
            msg['To'] = self.config.get('alert_to', '')

            with smtplib.SMTP(self.config['smtp_host'], self.config.get('smtp_port', 587)) as server:
                server.starttls()
                if self.config.get('smtp_user'):
                    server.login(self.config['smtp_user'], self.config['smtp_pass'])
                server.send_message(msg)

            logger.info(f"Alert email sent to {self.config.get('alert_to')}")
        except Exception as e:
            logger.error(f'Failed to send alert email: {e}')

    def get_last_alert(self):
        return {
            'last_alert_time': self.last_alert_time.isoformat() if self.last_alert_time else None,
        }

    def selfcheck(self):
        if not self.config.get('smtp_host'):
            return {'ok': True, 'message': 'SMTP not configured (alerts log-only)'}
        try:
            with smtplib.SMTP(self.config['smtp_host'], self.config.get('smtp_port', 587), timeout=5) as server:
                server.ehlo()
            return {'ok': True, 'message': 'SMTP server reachable'}
        except Exception as e:
            return {'ok': False, 'message': f'SMTP unreachable: {e}'}
