from django.dispatch import receiver, Signal

pulse = Signal()      # signal ALSO named "pulse" (cross-mechanism name collision)


@receiver(pulse)
def on_pulse(sender, **kw):
    pass


def fire_signal():
    pulse.send(sender=1)   # signal fire -> must reach ONLY the receiver
