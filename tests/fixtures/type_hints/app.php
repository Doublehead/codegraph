<?php

class Mailer { function send() {} }
class Other2 { function send() {} }     // decoy

function notify(Mailer $m) {            // typed param -> inferred to Mailer.send
    $m->send();
}

function make() {                       // local construction -> exact to Mailer.send
    $x = new Mailer();
    $x->send();
}

class Svc {
    // PHP 8 constructor promotion: promoted typed param -> inferred to Mailer.send
    public function __construct(private Mailer $m) {
        $m->send();
    }
}
