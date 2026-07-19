<?php
class Voice {
    public function register() {
        register_rest_route( Actions::NS, '/voice-token', array(
            'callback' => array( $this, 'handle_token' ),
            'permission_callback' => '__return_true',
        ) );
    }
    public function handle_token() {}
}
