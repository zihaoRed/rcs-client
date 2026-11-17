use std::env;
use std::fs;

use mls_rs::group::ReceivedMessage;
use mls_rs::identity::x509::{
    CertificateChain, DerCertificate, SubjectIdentityExtractor, X509CertificateReader,
    X509CredentialValidator, X509IdentityError,
};
use mls_rs::mls_rs_codec::MlsEncode;
use mls_rs::storage_provider::in_memory::InMemoryGroupStateStorage;
use mls_rs::{Client, Group, MlsMessage};
use mls_rs_core::crypto::SignaturePublicKey;
use mls_rs_core::error::IntoAnyError;
use mls_rs_core::extension::ExtensionList;
use mls_rs_core::group::{GroupState, GroupStateStorage};
use mls_rs_core::identity::{
    Credential, CredentialType, IdentityProvider, MemberValidationContext, SigningIdentity,
};
use mls_rs_core::time::MlsTime;
use mls_rs_crypto_openssl::x509::X509Reader;
use mls_rs_crypto_rustcrypto::RustCryptoProvider;

#[derive(Clone, Debug)]
struct AcceptAnyChain;

impl X509CredentialValidator for AcceptAnyChain {
    type Error = X509IdentityError;

    fn validate_chain(
        &self,
        chain: &CertificateChain,
        _timestamp: Option<MlsTime>,
    ) -> Result<SignaturePublicKey, Self::Error> {
        let leaf: &DerCertificate = chain
            .get(0)
            .ok_or(X509IdentityError::EmptyCertificateChain)?;
        X509Reader::new()
            .public_key(leaf)
            .map_err(|e| X509IdentityError::X509ReaderError(e.into_any_error()))
    }
}

#[derive(Clone, Debug)]
struct RcsIdentityProvider;

impl IdentityProvider for RcsIdentityProvider {
    type Error = X509IdentityError;

    fn validate_member(
        &self,
        signing_identity: &SigningIdentity,
        timestamp: Option<MlsTime>,
        _context: MemberValidationContext<'_>,
    ) -> Result<(), Self::Error> {
        if let Credential::X509(chain) = &signing_identity.credential {
            let pk = AcceptAnyChain.validate_chain(chain, timestamp)?;
            if pk != signing_identity.signature_key {
                return Err(X509IdentityError::SignatureKeyMismatch);
            }
        }
        Ok(())
    }

    fn validate_external_sender(
        &self,
        _signing_identity: &SigningIdentity,
        _timestamp: Option<MlsTime>,
        _extensions: Option<&ExtensionList>,
    ) -> Result<(), Self::Error> {
        Ok(())
    }

    fn identity(
        &self,
        signing_identity: &SigningIdentity,
        _extensions: &ExtensionList,
    ) -> Result<Vec<u8>, Self::Error> {
        match &signing_identity.credential {
            Credential::X509(chain) => {
                SubjectIdentityExtractor::new(0, X509Reader::new()).identity(chain)
            }
            Credential::Basic(basic) => Ok(basic.identifier.clone()),
            Credential::Custom(custom) => Ok(custom.data.clone()),
            _ => Ok(Vec::new()),
        }
    }

    fn valid_successor(
        &self,
        _predecessor: &SigningIdentity,
        _successor: &SigningIdentity,
        _extensions: &ExtensionList,
    ) -> Result<bool, Self::Error> {
        Ok(true)
    }

    fn supported_types(&self) -> Vec<CredentialType> {
        (0u16..=255).map(CredentialType::new).collect()
    }
}

fn hex(b: &[u8], max: usize) -> String {
    let n = b.len().min(max);
    let mut s = String::new();
    for x in &b[..n] {
        s.push_str(&format!("{:02x}", x));
    }
    if b.len() > n {
        s.push_str("...");
    }
    s
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!(
            "usage:\n  mls_probe <snapshot> <group_id> [mls_message.bin]\n  mls_probe <snapshot> <group_id> encrypt <payload.bin> <out_mls.bin> [out_snapshot.bin]"
        );
        std::process::exit(2);
    }
    let snapshot_path = &args[1];
    let gid = args[2].as_bytes().to_vec();
    let data = fs::read(snapshot_path)?;
    println!("snapshot bytes: {} group_id: {}", data.len(), args[2]);

    let mut storage = InMemoryGroupStateStorage::new();
    storage.write(GroupState { id: gid.clone(), data }, Vec::new(), Vec::new())?;
    let storage_view = storage.clone();

    let client = Client::builder()
        .identity_provider(RcsIdentityProvider)
        .crypto_provider(RustCryptoProvider::default())
        .group_state_storage(storage)
        .build();

    let mut group: Group<_> = client.load_group(&gid)?;
    println!(
        "LOADED group epoch={} cipher_suite={:?}",
        group.context().epoch,
        group.context().cipher_suite
    );

    match args.get(3).map(String::as_str) {
        Some("roster") | Some("info") => {
            println!("CONTEXT {:?}", group.context());
            for m in group.roster().members() {
                println!(
                    "MEMBER index={} identity={:?}",
                    m.index, m.signing_identity.credential
                );
            }
        }
        Some("encrypt") | Some("encrypt-aad") => {
            let with_aad = args.get(3).map(String::as_str) == Some("encrypt-aad");
            let payload_path = args.get(4).ok_or("missing payload path")?;
            let (aad, out_idx) = if with_aad {
                let aad_path = args.get(5).ok_or("missing aad path")?;
                (fs::read(aad_path)?, 6)
            } else {
                (Vec::new(), 5)
            };
            let out_mls = args.get(out_idx).ok_or("missing out mls path")?;
            let payload = fs::read(payload_path)?;
            let mls = group.encrypt_application_message(&payload, aad)?;
            let bytes = mls.mls_encode_to_vec()?;
            fs::write(out_mls, &bytes)?;
            println!("ENCRYPTED payload={} mls={} -> {}", payload.len(), bytes.len(), out_mls);
            if let Some(out_snapshot) = args.get(out_idx + 1) {
                group.write_to_storage()?;
                if let Some(state) = storage_view.state(&gid)? {
                    fs::write(out_snapshot, &state)?;
                    println!("SNAPSHOT {} bytes -> {}", state.len(), out_snapshot);
                }
            }
        }
        Some(msg_path) => {
            let bytes = fs::read(msg_path)?;
            let msg = MlsMessage::from_bytes(&bytes)?;
            println!("MLS message: {:?}", msg.description());
            match group.process_incoming_message(msg)? {
                ReceivedMessage::ApplicationMessage(desc) => {
                    println!(
                        "APP DATA ({} bytes): {}",
                        desc.data().len(),
                        String::from_utf8_lossy(desc.data())
                    );
                    println!("APP HEX: {}", hex(desc.data(), 128));
                    println!(
                        "AAD ({} bytes): {}",
                        desc.authenticated_data.len(),
                        hex(&desc.authenticated_data, 256)
                    );
                    println!(
                        "AAD ASCII: {}",
                        String::from_utf8_lossy(&desc.authenticated_data)
                    );
                    if let Some(out_app) = args.get(5) {
                        fs::write(out_app, desc.data())?;
                        println!("APPDATA {} bytes -> {}", desc.data().len(), out_app);
                    }
                }
                other => println!("received: {other:?}"),
            }
            if let Some(out_snapshot) = args.get(4) {
                group.write_to_storage()?;
                if let Some(state) = storage_view.state(&gid)? {
                    fs::write(out_snapshot, &state)?;
                    println!("SNAPSHOT {} bytes -> {}", state.len(), out_snapshot);
                }
            }
        }
        None => {}
    }

    Ok(())
}
