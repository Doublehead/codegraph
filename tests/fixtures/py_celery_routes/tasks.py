from celery import shared_task


@shared_task
def email_user(uid):
    pass


@shared_task(bind=True)
def sync_data(self):
    pass


def trigger():
    email_user.delay(7)          # fire -> caller->task edge
    sync_data.apply_async()      # fire -> caller->task edge


def not_a_task(animation):
    animation.delay(5)           # `animation` is not a @task -> must NOT fabricate an edge
