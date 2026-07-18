def run(handler):
    handler()

def rebound():
    notify = object()
    notify()

def nested_case():
    def inner():
        pass
    inner()
