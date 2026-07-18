from core import save, Repo

def handler():
    r = Repo()
    return r.save()

def boot():
    return save()
