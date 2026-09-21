"""QQ 邮箱异步通知: 后台线程 + 队列, 不阻塞交易主循环。

环境变量:
  MSG_SMTP_USERNAME  发件邮箱 (如 123456@qq.com), 别名 QQ_SMTP_USER
  MSG_SMTP_PASSWORD  SMTP 授权码,             别名 QQ_SMTP_SECRET
  QQ_NOTIFY_TO       收件邮箱 (默认发件邮箱自身)
  ENABLE_EMAIL_MSG   true/false (默认 true)
"""

import logging
import os
import queue
import smtplib
import threading
from email.header import Header
from email.mime.text import MIMEText

log = logging.getLogger("ztp")

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465


class Mailer:
    def __init__(self):
        self.user = os.environ.get("MSG_SMTP_USERNAME") or os.environ.get("QQ_SMTP_USER", "")
        self.secret = os.environ.get("MSG_SMTP_PASSWORD") or os.environ.get("QQ_SMTP_SECRET", "")
        self.to_addr = os.environ.get("QQ_NOTIFY_TO") or self.user
        flag = os.environ.get("ENABLE_EMAIL_MSG", "true").strip().lower()
        self.enabled = bool(self.user and self.secret) and flag in ("1", "true", "yes", "on")
        self._q: queue.Queue = queue.Queue(maxsize=50)
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="ztp-mailer").start()

    def send(self, subject: str, body: str = ""):
        if not self.enabled:
            log.debug("邮件通知未启用, 跳过: %s", subject)
            return
        try:
            self._q.put_nowait((subject[:120], body))
        except queue.Full:
            log.warning("邮件队列已满, 丢弃: %s", subject)

    def _worker(self):
        while True:
            subject, body = self._q.get()
            ok = False
            for attempt in range(3):
                try:
                    self._smtp_send(subject, body)
                    ok = True
                    break
                except Exception as e:
                    log.warning("邮件发送失败(第%d次): %s | %s", attempt + 1, subject, e)
                    threading.Event().wait(3 * (attempt + 1))
            if not ok:
                log.error("邮件最终发送失败: %s", subject)

    def _smtp_send(self, subject: str, body: str):
        msg = MIMEText(body or subject, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = self.user
        msg["To"] = self.to_addr
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.login(self.user, self.secret)
            s.sendmail(self.user, [self.to_addr], msg.as_string())
        log.info("邮件已发送: %s", subject)
