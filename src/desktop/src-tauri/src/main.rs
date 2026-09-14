// The window. Everything it does is in the library beside this file, so that the supervisor,
// the proxy and the first-run seeding can be unit-tested without opening one.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    mcp_gateway_desktop::run();
}
