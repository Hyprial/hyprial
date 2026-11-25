# OC3 organization cell membership

An organization cell is owned by one `user:u.<sub>` authority. The owner
actor writes the roster and the Tailnet authority. Member daemons keep a
read-only projection and can announce only their own device.

Roster entries use `uidRef`, `username`, `role`, `joinedAtMs`, and device
records. Device identifiers are unique across the roster. The owner cell is
bounded to 64 members. OIDC joins bind `iss`, `sub`, and
`preferred_username` atomically to the owner roster. A consumed join token
cannot be reused.

Casdoor remains available for login compatibility and historical reads. It is
not a membership authority and does not grant cell or Tailnet access.
