<?php
class Actions {
    const NS = 'zrougable/v1';

    public function register() {
        register_rest_route( self::NS, '/scene', array(
            'callback' => array( $this, 'route_scene' ),
            'permission_callback' => '__return_true',
        ) );
    }
    public function route_scene() {}
}
