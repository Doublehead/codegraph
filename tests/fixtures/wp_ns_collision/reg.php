<?php
// AMBIGUOUS: A\Mailer::send and B\Mailer::send both exist and both store as bare
// `Mailer.send`. The FQN says B, but symbols are stored bare so it cannot be told
// apart here -> must NOT guess (callback stays unresolved). Contract: never WRONG.
add_action('collide', ['\\B\\Mailer', 'send']);

// UNAMBIGUOUS: Notifier is a unique class name -> the bare fallback is the only
// candidate and must still resolve. Proves the P1 fix didn't over-correct.
add_action('unique', ['\\B\\Notifier', 'ping']);
