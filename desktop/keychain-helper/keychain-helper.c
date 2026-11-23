/*
 * Build with clang -mmacosx-version-min=12.0 -Wno-deprecated-declarations
 * -framework Security -framework CoreFoundation keychain-helper.c
 * -o keychain-helper. Keep the Developer ID
 * team and signing identifier (com.maximizepm.keychain-helper) stable.
 *
 * Usage: keychain-helper put|get|delete SERVICE ACCOUNT.
 * put reads a nonempty value to EOF, get writes exact bytes, and delete
 * is idempotent.
 * Exit 0 means success, 1 means an error, 2 means a missing get, and 64
 * means incorrect arguments. Diagnostics go to stderr and omit the data.
 *
 * Use the file-based Keychain APIs for per-application access lists.
 * macOS lets other programs delete items without an access prompt. The ACL
 * authenticates the helper executable, so a process that can run the helper
 * (including a replacement for maxpm serve outside its sandbox) can read
 * through it. Ad-hoc signatures are for dummy-data tests.
 */

#define __STDC_WANT_LIB_EXT1__ 1
#include <Security/Security.h>
#include <stdio.h>
#include <string.h>

/* Room for the vault's 64 KiB limit after base64 encoding and its b64: prefix. */
#define MAX_VALUE_BYTES (4 * ((64 * 1024 + 2) / 3) + 4)

static OSStatus make_access(SecAccessRef *access)
{
    SecTrustedApplicationRef self = NULL;
    OSStatus status = SecTrustedApplicationCreateFromPath(NULL, &self);
    if (status != errSecSuccess)
        return status;

    const void *apps[] = {self};
    CFArrayRef trusted = CFArrayCreate(NULL, apps, 1, &kCFTypeArrayCallBacks);
    CFRelease(self);
    if (trusted == NULL)
        return errSecAllocate;

    status = SecAccessCreate(CFSTR("MaximizePM vault"), trusted, access);
    CFRelease(trusted);
    return status;
}

static OSStatus find_item(SecKeychainRef keychain, const char *service,
                          const char *account, SecKeychainItemRef *item)
{
    return SecKeychainFindGenericPassword(keychain, (UInt32)strlen(service), service,
                                          (UInt32)strlen(account), account,
                                          NULL, NULL, item);
}

static void free_value(void *data, UInt32 length)
{
    if (data != NULL) {
        memset_s(data, length, 0, length);
        SecKeychainItemFreeContent(NULL, data);
    }
}

static OSStatus replace_value(SecKeychainItemRef item, UInt32 length, const void *data)
{
    /* Preserve the existing ACL only if the helper is its sole trusted reader.
     * The actual read also checks the signature and Keychain partition.
     * The vault will migrate items created by the legacy backend separately.
     */
    SecAccessRef access = NULL;
    OSStatus status = SecKeychainItemCopyAccess(item, &access);
    if (status != errSecSuccess)
        return status;

    CFArrayRef decrypt = SecAccessCopyMatchingACLList(access, kSecACLAuthorizationDecrypt);
    CFRelease(access);
    SecKeychainPromptSelector prompt;
    CFArrayRef apps = NULL;
    CFStringRef description = NULL;
    status = errSecAuthFailed;
    if (decrypt != NULL) {
        if (CFArrayGetCount(decrypt) == 1)
            status = SecACLCopyContents((SecACLRef)CFArrayGetValueAtIndex(decrypt, 0),
                                        &apps, &description, &prompt);
        CFRelease(decrypt);
    }
    if (description != NULL)
        CFRelease(description);
    bool single_reader = apps != NULL && CFArrayGetCount(apps) == 1;
    if (apps != NULL)
        CFRelease(apps);
    if (status != errSecSuccess)
        return status;
    if (!single_reader)
        return errSecAuthFailed;

    void *old_data = NULL;
    UInt32 old_length = 0;
    status = SecKeychainItemCopyContent(item, NULL, NULL, &old_length, &old_data);
    if (status != errSecSuccess)
        return status;
    free_value(old_data, old_length);
    /* Keychain can return empty data without checking decrypt permission. */
    if (old_length == 0)
        return errSecAuthFailed;
    return SecKeychainItemModifyAttributesAndData(item, NULL, length, data);
}

static OSStatus store(SecKeychainRef keychain, char *service, char *account,
                      UInt32 length, const void *data)
{
    SecAccessRef access = NULL;
    OSStatus status = make_access(&access);
    if (status != errSecSuccess)
        return status;

    SecKeychainAttribute fields[] = {
        {kSecServiceItemAttr, (UInt32)strlen(service), service},
        {kSecAccountItemAttr, (UInt32)strlen(account), account},
    };
    SecKeychainAttributeList attributes = {2, fields};
    status = SecKeychainItemCreateFromContent(kSecGenericPasswordItemClass,
                                              &attributes, length, data, keychain,
                                              access, NULL);
    if (status == errSecDuplicateItem) {
        SecKeychainItemRef item = NULL;
        status = find_item(keychain, service, account, &item);
        if (status == errSecSuccess) {
            status = replace_value(item, length, data);
            CFRelease(item);
        }
    }
    CFRelease(access);
    return status;
}

static OSStatus put(SecKeychainRef keychain, char *service, char *account)
{
    unsigned char data[MAX_VALUE_BYTES + 1];
    size_t length = fread(data, 1, sizeof(data), stdin);
    OSStatus status;
    if (ferror(stdin) || length == 0 || length > MAX_VALUE_BYTES) {
        fputs("keychain-helper: could not read a nonempty value within the vault size limit\n", stderr);
        status = errSecIO;
    } else {
        status = store(keychain, service, account, (UInt32)length, data);
    }
    memset_s(data, sizeof(data), 0, sizeof(data));
    return status;
}

static OSStatus write_value(SecKeychainItemRef item)
{
    void *data = NULL;
    UInt32 length = 0;
    OSStatus status = SecKeychainItemCopyContent(item, NULL, NULL, &length, &data);
    if (status != errSecSuccess)
        return status;

    if ((length > 0 && fwrite(data, 1, length, stdout) != length) || fflush(stdout) == EOF)
        status = errSecIO;
    free_value(data, length);
    return status;
}

int main(int argc, char **argv)
{
    if (argc != 4 || !argv[2][0] || !argv[3][0] ||
        (strcmp(argv[1], "put") && strcmp(argv[1], "get") && strcmp(argv[1], "delete"))) {
        fputs("usage: keychain-helper put|get|delete SERVICE ACCOUNT\n", stderr);
        return 64;
    }

    const char *command = argv[1];
    char *service = argv[2];
    char *account = argv[3];
    SecKeychainRef keychain = NULL;
    OSStatus status = SecKeychainSetUserInteractionAllowed(false);
    if (status == errSecSuccess)
        status = SecKeychainCopyDefault(&keychain);
    if (status == errSecSuccess) {
        if (strcmp(command, "put") == 0) {
            status = put(keychain, service, account);
        } else {
            bool deleting = strcmp(command, "delete") == 0;
            SecKeychainItemRef item = NULL;
            status = find_item(keychain, service, account, &item);
            if (status == errSecSuccess) {
                status = deleting ? SecKeychainItemDelete(item) : write_value(item);
                CFRelease(item);
            }
            if (status == errSecItemNotFound) {
                CFRelease(keychain);
                return deleting ? 0 : 2;
            }
        }
        CFRelease(keychain);
    }

    if (status == errSecSuccess)
        return 0;
    fprintf(stderr, "keychain-helper: %s failed (OSStatus %d)\n", command, (int)status);
    return 1;
}
