<?php
class Builder {
    public function register( $ns ) {
        register_rest_route( $ns . '/builder', '/save', array(
            'callback' => array( $this, 'route_save' ),
        ) );
    }
    public function route_save() {}
}
