<?php
namespace B;

class Mailer {
    public function send() {}
}

// Unique class name -> a namespaced callback to it is UNAMBIGUOUS and must resolve.
class Notifier {
    public function ping() {}
}
