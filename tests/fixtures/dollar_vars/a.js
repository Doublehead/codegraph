class El { m() {} }
class Foo { m() {} }

function f() {
    let $el = new El();
    let el = new Foo();
    $el.m();   // must resolve to El.m
    el.m();    // must resolve to Foo.m
}
