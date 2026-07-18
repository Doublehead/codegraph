class Request:
    def json(self): pass


class Other:           # decoy: same method name, must NOT be blended into a hint edge
    def json(self): pass


class Foo:
    def run(self): pass


class Iface:
    def go(self): pass


class ImplA(Iface):    # decoy implementor; a hint to the interface must not pick this
    def go(self): pass


def injected(req: Request):    # typed param -> inferred edge to Request.json
    req.json()


def built():                   # local construction -> exact edge to Foo.run
    x = Foo()
    x.run()


def via_interface(x: Iface):   # interface hint -> inferred to Iface.go, never ImplA.go
    x.go()
