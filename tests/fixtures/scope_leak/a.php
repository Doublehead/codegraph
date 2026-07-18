<?php
class Account { public function save() {} }
class AuditLog { public function save() {} }

function persist(Account $x) {
    $cb = function() { $x = new AuditLog(); };  // closure shadow leaks into persist's scope
    $x->save();   // CONFLICT: param hint Account vs leaked closure construct AuditLog
}

function clean(Account $y) {
    $cb = function() { $z = new AuditLog(); };   // different name -> no conflict
    $y->save();   // clean hint -> inferred Account.save
}
