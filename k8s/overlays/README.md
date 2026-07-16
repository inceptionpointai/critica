# k8s/overlays

## AWS overlays (`staging/` and `prod/`)

These are the pre-migration AWS/EKS kustomize overlays, retained
intentionally for cloud portability -- the org migrated this service's
traffic to GKE, but nothing here has been deleted in case AWS infra is
ever needed again.

- **Not rendered by any ArgoCD Application.** The live ArgoCD apps for
  this service read `staging-gke/` and `prod-gke/` (Artifact Registry images, GKE-native
  manifests). These plain AWS overlays have no ArgoCD consumer today.
- **The `-gke` siblings are live.** `deploy-gke.yml` is the live pipeline shipping staging; prod-gke is promoted by pinning the image tag directly in `prod-gke/kustomization.yaml` (no automated prod-gke workflow exists yet).
- **Contents reference decommissioned AWS infra.** Image tags point at
  ECR (`567274077914.dkr.ecr.us-west-2.amazonaws.com/ipoint-critica-prod`), and
  IAM roles/ARNs baked into these manifests and the (disabled) CI workflows that used to populate them
  target an AWS account whose ECR repos and EKS cluster no longer exist.
  A return to AWS would need real rehab here -- new ECR repos, IAM roles,
  DNS, etc. -- not just re-enabling the old workflow jobs.
