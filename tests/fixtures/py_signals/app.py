from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver, Signal

order_done = Signal()       # custom signal


@receiver(post_save)
def cache_user(sender, **kw):     # listens on post_save (decorator form)
    pass


@receiver([post_save, post_delete])
def on_change(sender, **kw):      # R1: list form -> listens on BOTH signals
    pass


def audit_user(sender, **kw):
    pass


def notify(sender, **kw):
    pass


def kw_handler(sender, **kw):
    pass


def noise(sender, **kw):
    pass


def wire():
    post_save.connect(audit_user)            # connect form, free cb
    order_done.connect(notify)               # custom signal
    post_save.connect(receiver=kw_handler)   # R2: keyword `receiver=` form


def do_stuff(sock):                # D1: `sock` is NOT a signal -> must NOT fabricate an edge
    sock.connect(noise)
    sock.send(b"x")


def save_user(u):
    u.persist()
    post_save.send(sender=u)           # fires post_save -> cache_user, on_change, audit_user, kw_handler, Mailer.on_save


def purge(o):
    post_delete.send(sender=o)         # fires post_delete -> on_change


def finish_order(o):
    order_done.send(sender=o)          # fires order_done -> notify (NOT the post_save handlers)


class Mailer:
    def setup(self):
        post_save.connect(self.on_save)   # method_self listener -> Mailer.on_save

    def on_save(self, sender, **kw):
        pass
