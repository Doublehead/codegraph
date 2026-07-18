<?php
function save() { return write(); }
function write() { return 1; }
class Repo {
    function store() { return $this->flush(); }
    function flush() { return save(); }
    function copy() { return self::flush2(); }
    function flush2() { return 2; }
}
function loopA() { return loopB(); }
function loopB() { return loopA(); }
