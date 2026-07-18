def save():
    return write()

def write():
    return 1

def load():
    return save()

class Repo:
    def save(self):
        return self._write()
    def _write(self):
        return save()
    def fetch(self):
        return self.save()

def outer():
    def inner():
        return helper()
    return inner()

def helper():
    return load()

def recurse(n):
    return recurse(n - 1)

def ping():
    return pong()

def pong():
    return ping()
