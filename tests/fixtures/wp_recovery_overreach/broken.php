<?php
// `const NAMESPACE` makes tree-sitter-php collapse this class into ERROR nodes,
// forcing the byte-level container recovery path.
class Broken {
    const NAMESPACE = 'triggers_parse_error';

    public function inside_method() {}
}

// Genuine top-level functions AFTER the broken class body. Recovery must NOT sweep
// these into Broken — they fall outside the brace-matched class body.
function top_level_helper() {}

function top_level_outside() {}
