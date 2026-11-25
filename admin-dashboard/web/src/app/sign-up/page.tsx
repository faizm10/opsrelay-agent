import { AccountForm } from "@/components/account-form";

export const metadata = { title: "Create an account | OpsRelay" };

export default function SignUpPage() {
  return <AccountForm mode="sign-up" />;
}
