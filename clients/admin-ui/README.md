# Admin UI

Admin UI for managing FidesOps privacy requests. A web application built in Next.js with the FidesUI component library.

## Running Locally

1. Run `make server` in top-level `fidesops` directory, then run `make user` and follow prompts to create a user. Note that password requires 8 or more characters, upper and lowercase chars, a number, and a symbol.
2. In a new shell, `cd` into `clients/admin-ui`, then run `npm run dev`.
3. Nav to `http://localhost:3000/` and logged in using created user. The `email` field is simply the `user` that was created, not a valid email address.

## Testing Entire Request Flow

1. Run the `fidesops` server with `make server`.
2. Create a policy key through the API (using the fidesops Postman collection).
3. Configure the `clients/privacy-center` application to use that policy by adding it to the appropriate request config in `config/config.json`.
4. Run the Privacy Request center using `npm run dev`.
5. Submit a privacy request through the Privacy Request center.
6. View that request in the Admin UI and either approve or deny it.

## Authentication

To enable stable authentication you must supply a `NEXTAUTH_SECRET` environment
variable. The best way to do this is by creating a `.env.local` file, which Next
will automatically pick up:

```bash
echo NEXTAUTH_SECRET=`openssl rand -base64 32` >> .env.local
```
