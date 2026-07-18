<?php
class SB_Plugin {
    function init() {
        add_action('rest_api_init', [$this, 'register_routes']);
        add_filter('the_content', 'sb_render', 10, 1);
        add_action('wp_ajax_nopriv_sb_save', [$this, 'ajax_save']);
        add_action('wp_ajax_sb_save', [$this, 'ajax_save']);
        register_rest_route('sb/v1', '/save', ['methods' => 'POST', 'callback' => [$this, 'rest_save'], 'permission_callback' => '__return_true']);
        add_action('sb_custom', 'SB_Helper::boot');
        add_action('init', function () { return 1; });
        add_action("sb_dynamic_{$x}", [$this, 'dyn']);
    }
    function register_routes() { return 1; }
    function ajax_save() { return 2; }
    function rest_save() { return 3; }
    function dyn() { return 4; }
    function fire_custom() { do_action('sb_custom'); }
}
class SB_Helper {
    static function boot() { return 5; }
}
function sb_render($content) { return $content; }
