<?php
class Plug {
    function reg() {
        add_action("dq_hook", "dq_free");                       // double-quoted name + callback
        add_action('h_self', [self::class, 'shared']);          // self::class
        add_action('h_static', [static::class, 'shared']);      // static::class
        add_action('h_nsarr', ['App\\Mailer', 'send']);         // namespaced array element
        add_action('h_nsstr', 'App\\Mailer::send');             // namespaced static string
        $obj = new Mailer();
        add_action('h_typed', [$obj, 'send']);                  // typed local var (collision: Mailer vs Other)
        \add_action('h_bslash', [$this, 'shared']);             // leading-backslash global call
        add_action('wp_ajax_nopriv_go', [$this, 'shared']);     // unauth ajax
        add_action('admin_post_nopriv_go', [$this, 'shared']);  // admin_post nopriv (UNAUTH)
        add_action('admin_post_go', [$this, 'shared']);         // admin_post (auth)
        register_rest_route('v1', '/pub', ['callback' => [$this, 'shared'], 'permission_callback' => '\\__return_true']);
    }
    function shared() {}
}
class Mailer { function send() {} }
class Other { function send() {} }
function dq_free() {}
