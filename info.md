Why HTTP "worked" but HTTPS doesn't: this isn't actually about your Java code or the protocol itself — it's a Windows/Office client-side security feature called Mark of the Web (MOTW). When a file is downloaded through a zone Windows considers "Internet" (typical for HTTPS endpoints, especially external-facing ones), Explorer/Office tags it with a zone identifier. When Excel opens a MOTW-tagged file, it does strict file signature validation: it looks at the actual binary header (a real .xls starts with the OLE Compound File signature D0 CF 11 E0..., while your renamed xlsx starts with the ZIP signature PK..). Mismatch → "the file format and extension don't match, the file could be corrupted" popup. Over HTTP/intranet zones this stricter check is often skipped or trusted, so the same mismatched file opens silently. So you're right that it's "nowhere relevant" to your Java logic — it's a client-side Office/Windows trust check triggered by how the file was fetched, not by your servlet code.

The only robust fix is to actually convert the xlsx bytes BO gives you into a true legacy BIFF8 workbook before writing it with a .xls extension

==================================================================================================================================================
it's a client/policy-level fix, not something you can do from your Java REST call or HTTP response headers — there's no header that tells Windows "don't mark this file with MOTW."

Why it's client-side, not server-side

MOTW (the Zone.Identifier alternate data stream Windows attaches to downloaded files) is applied by the browser/Windows Attachment Manager based on which Security Zone the source URL belongs to — it has nothing to do with any header your servlet sends. The zone is resolved from IE's Site-to-Zone mapping (yes, even if users browse with Chrome/Edge, Windows still consults this mapping for MOTW purposes):

Hosts matched by short/unqualified names, or explicitly listed as Local Intranet / Trusted Sites, get little or no MOTW enforcement.
Anything not explicitly mapped defaults to the Internet zone, which triggers MOTW + Protected View + strict file-signature validation in Office.

This lines up exactly with what you saw: your HTTP endpoint is probably reached via a short intranet hostname (auto-classified as Local Intranet), while the HTTPS endpoint is reached via a fully-qualified external-looking domain, which Windows defaults to the Internet zone.


Feasible fix (if you want to go this route instead of/alongside the POI conversion)

Get your BO server's HTTPS hostname added to the Local Intranet or Trusted Sites zone list, pushed via Group Policy:

Computer Configuration → Administrative Templates → Windows Components → Internet Explorer → Internet Control Panel → Security Page → Site to Zone Assignment List

Add an entry like https://your-bo-host.company.com → zone 1 (Intranet) or 2 (Trusted Sites). This is domain-wide, doesn't touch your Java code, and is the standard enterprise pattern for exactly this problem (internal web apps whose downloads trigger unwanted Protected View/warnings).

What I'd avoid

There's also a GPO called "Do not preserve zone information in file attachments" (SaveZoneInformation), which globally disables MOTW tagging for the machine/user. Technically it "solves" this, but it's a blanket security downgrade — it removes the warning for genuinely malicious downloaded files too, not just your reports. I wouldn't recommend it just to fix one report export flow.

One caveat either way

Even if you suppress MOTW via zone mapping, the file is still not actually a legacy .xls — it's a zip/xlsx renamed. Some Office builds also apply "extension hardening" checks independent of MOTW under certain security configurations, so the warning could resurface later with an Office update or a stricter org policy, regardless of zone settings. The zone-mapping fix is legitimate and low-effort, but it's masking the mismatch rather than removing it — the POI conversion I gave you removes the mismatch at the source, so it stays fixed no matter what Windows/Office does with zones in the future. If you can get IT to add the site-to-zone entry, I'd do both: it's a nice belt-and-suspenders combo, but I wouldn't rely on zone mapping alone as your only fix.
