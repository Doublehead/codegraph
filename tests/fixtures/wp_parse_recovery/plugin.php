<?php
class Static_Booking_REST_API {
    const NAMESPACE = 'static-booking/v1';
    public function register_routes() {
        register_rest_route(self::NAMESPACE, '/cancel', [
            'callback' => [$this, 'cancel_booking_public'],
            'permission_callback' => '__return_true',
        ]);
    }
    public function cancel_booking_public() { return 1; }
    public function uses_helper() { return $this->cancel_booking_public(); }
}
class Static_Booking_Availability {
    public function cancel_booking_public() { return 2; }
}
