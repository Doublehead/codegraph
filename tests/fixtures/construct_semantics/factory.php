<?php

function Make() { return new Other(); }   // global factory function

class Make { function build() {} }          // class colliding with the function name
class Other { function build() {} }
class Thing { function build() {} }

function run_it() {
    return Make()->build();   // function-call receiver: unverified return -> ambiguous, not Make.build exact
}
