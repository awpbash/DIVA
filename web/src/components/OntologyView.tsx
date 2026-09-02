import {
  addOntologyField, deleteOntologyField, editOntologyField, getOntology, setCategorySensitivity,
} from "../api";
import { OntologyEditor } from "./OntologyEditor";

// The admin Ontology tab: OntologyEditor wired to the authenticated /ontology/*
// routes. The setup wizard's clone-and-edit step (SetupWizard.tsx) renders the
// same OntologyEditor wired to the unauthenticated /setup/ontology/* passthrough
// instead — see OntologyEditor.tsx's module comment for why one component covers
// both.
export function OntologyView() {
  return (
    <OntologyEditor
      fetchOntology={getOntology}
      addField={addOntologyField}
      editField={editOntologyField}
      deleteField={deleteOntologyField}
      setSensitivity={setCategorySensitivity}
    />
  );
}
