<?php
class Helper {
    function go() { return save(); }
    function dup() { return $this->go(); }
}
