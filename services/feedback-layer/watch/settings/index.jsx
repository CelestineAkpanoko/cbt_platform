// Phone-side settings page: which participant this watch shows, and the
// deployed Prediction API base URL. Read by companion/index.js.
function Settings() {
  return (
    <Page>
      <Section title={<Text bold>CBT Heat Alert</Text>}>
        <TextInput label="user_id (e.g. user15)" settingsKey="userId" />
        <TextInput label="Organisation id (e.g. org1)" settingsKey="orgId" />
        <TextInput label="Prediction API base URL" settingsKey="apiBase" />
      </Section>
    </Page>
  );
}
registerSettingsPage(Settings);
