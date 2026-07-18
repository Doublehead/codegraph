from celery import shared_task


@shared_task
def pulse():          # task named "pulse"
    pass


def fire_task():
    pulse.delay()     # task fire -> must reach ONLY the task body
