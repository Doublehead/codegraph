from typing import List

db = Database()             # module-level construct


class Database:
    def query(self): pass


class Other:
    def query(self): pass


class Logger:
    def write(self): pass


class Real:
    def write(self): pass


class Service:
    def run(self): pass


def Service():              # free function colliding with class Service (D4)
    return 1


def handler(db: List[Other]):    # D1: generic-typed param shadows module `db` -> ambiguous
    db.query()


def collide(Logger: List[int]):  # D1: param name == class, generic type -> not STATIC exact
    Logger.write()


def use_service():               # D4: x = Service() ambiguous class/func -> inferred
    x = Service()
    x.run()
