import java.io.*;
import java.net.URL;
import java.util.HashMap;
import java.util.Map;

import javax.net.ssl.HttpsURLConnection;

import org.apache.poi.hssf.usermodel.*;
import org.apache.poi.hssf.util.HSSFColor;
import org.apache.poi.ss.usermodel.FillPatternType;
import org.apache.poi.ss.util.CellRangeAddress;
import org.apache.poi.xssf.usermodel.*;

/**
 * Fix summary
 * -----------
 * SAP BO's raylight REST API does NOT produce a genuine legacy BIFF8 (.xls)
 * binary for WebI documents — it always returns OOXML (.xlsx) bytes, even
 * when the Accept header says "application/vnd.ms-excel". The previous
 * implementation just renamed those xlsx bytes to *.xls, which happened to
 * "work" over HTTP because Excel/Windows didn't apply strict file-signature
 * validation on files that weren't tagged with Mark-of-the-Web (MOTW).
 * Over HTTPS (or any download flow that gets tagged Internet-zone), Excel
 * checks the actual binary signature against the extension, sees a ZIP (PK..)
 * signature inside a ".xls" file instead of an OLE/BIFF8 signature
 * (D0 CF 11 E0...), and throws the "file format and extension don't match /
 * file may be corrupted" warning.
 *
 * The only reliable fix is to actually convert the xlsx bytes into a real
 * BIFF8 workbook (HSSFWorkbook) before writing the .xls file, so the bytes
 * genuinely match the extension. That's what convertXlsxToLegacyXls() does
 * below — it also enforces BIFF8's real 65,536-row / 256-column ceiling
 * (instead of silently truncating/corrupting).
 *
 * Maven dependencies required:
 *   org.apache.poi:poi:5.x        (HSSF - legacy .xls)
 *   org.apache.poi:poi-ooxml:5.x  (XSSF - .xlsx)
 */
public class ExportAPIFix {

    private static final int XLS_MAX_ROWS = 65536; // BIFF8 hard limit
    private static final int XLS_MAX_COLS = 256;   // BIFF8 hard limit (col IV)

    // Replace with your actual logger (e.g. org.slf4j.Logger)
    private static final java.util.logging.Logger logger =
            java.util.logging.Logger.getLogger(ExportAPIFix.class.getName());

    // 5th call
    public void exportAPI(String outReportName, String baseUrl, Integer intReportIdBO,
                           String logonToken, String ext) {
        try {
            URL reportDownloadUrl;
            if (ext.equals(".pdf")) {
                reportDownloadUrl = new URL(baseUrl + "/biprws/raylight/v1/documents/"
                        + intReportIdBO + "/pages?mode=normal");
            } else {
                reportDownloadUrl = new URL(baseUrl + "/biprws/raylight/v1/documents/" + intReportIdBO);
            }
            System.out.println(reportDownloadUrl);

            HttpsURLConnection reportDownloadConn = (HttpsURLConnection) reportDownloadUrl.openConnection();
            System.out.println(reportDownloadConn);
            reportDownloadConn.setRequestMethod("GET");
            reportDownloadConn.setRequestProperty("X-SAP-LogonToken", logonToken);
            reportDownloadConn.setRequestProperty("Content-Type", "application/json");

            // NOTE: BO has no native "xls" export. When the caller asks for
            // ".xls" we still request xlsx from BO (that's genuinely all it
            // can produce for WebI), then convert it for real after download.
            boolean wantsLegacyXls = ext.equals(".xls");

            if (wantsLegacyXls || ext.equals(".xlsx")) {
                reportDownloadConn.setRequestProperty("Accept",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
            } else if (ext.equals(".pdf")) {
                reportDownloadConn.setRequestProperty("Accept", "application/pdf");
            } else if (ext.equals(".csv")) {
                reportDownloadConn.setRequestProperty("Accept", "text/csv");
            } else {
                reportDownloadConn.setRequestProperty("Accept",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
            }

            reportDownloadConn.setRequestProperty("Connection", "Keep-Alive");
            reportDownloadConn.setConnectTimeout(18000000);
            reportDownloadConn.setReadTimeout(18000000);
            System.out.println(reportDownloadConn.getHeaderFields());

            if (reportDownloadConn.getResponseCode() != 200) {
                throw new RuntimeException("Failed : HTTP error code : "
                        + reportDownloadConn.getResponseCode()
                        + reportDownloadConn.getResponseMessage() + reportDownloadConn.getHeaderFields());
            }

            String filePath = outReportName + ext;

            if (wantsLegacyXls) {
                // ---- Real fix: buffer the xlsx bytes, convert to true BIFF8, then write ----
                byte[] xlsxBytes = readAllBytes(reportDownloadConn.getInputStream());
                byte[] legacyXlsBytes = convertXlsxToLegacyXls(new ByteArrayInputStream(xlsxBytes));

                try (OutputStream out = new BufferedOutputStream(new FileOutputStream(filePath))) {
                    out.write(legacyXlsBytes);
                    out.flush();
                }
            } else {
                // Unchanged path for pdf / csv / xlsx / others: stream straight through
                try (InputStream in = reportDownloadConn.getInputStream();
                     OutputStream out = new BufferedOutputStream(new FileOutputStream(filePath))) {
                    byte[] buffer = new byte[4096];
                    int len;
                    while ((len = in.read(buffer, 0, buffer.length)) != -1) {
                        out.write(buffer, 0, len);
                    }
                    out.flush();
                }
            }

            reportDownloadConn.disconnect();
            if (baseUrl != null && !baseUrl.isEmpty() && logonToken != null) {
                logger.info("BO Session closed !!");
                // logoffBO(baseUrl, logonToken);
            }

        } catch (java.net.MalformedURLException e) {
            e.printStackTrace();
        } catch (IOException e) {
            e.printStackTrace();
        }
    }

    private byte[] readAllBytes(InputStream in) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int len;
        while ((len = in.read(buffer)) != -1) {
            bos.write(buffer, 0, len);
        }
        return bos.toByteArray();
    }

    /**
     * Converts genuine xlsx bytes (what BO actually returns) into a genuine
     * legacy BIFF8 (.xls) workbook, preserving values, formulas, basic
     * styling (font, fill, borders, alignment), merged regions and column
     * widths — bounded by BIFF8's real limits.
     */
    private byte[] convertXlsxToLegacyXls(InputStream xlsxInputStream) throws IOException {
        try (XSSFWorkbook xssfWorkbook = new XSSFWorkbook(xlsxInputStream);
             HSSFWorkbook hssfWorkbook = new HSSFWorkbook()) {

            Map<Integer, HSSFCellStyle> styleCache = new HashMap<>();

            for (int s = 0; s < xssfWorkbook.getNumberOfSheets(); s++) {
                XSSFSheet xSheet = xssfWorkbook.getSheetAt(s);
                int lastRow = xSheet.getLastRowNum();

                if (lastRow >= XLS_MAX_ROWS) {
                    logger.warning("Sheet '" + xSheet.getSheetName() + "' has " + (lastRow + 1)
                            + " rows; legacy XLS supports only " + XLS_MAX_ROWS
                            + ". Extra rows will be dropped in the .xls output.");
                }

                HSSFSheet hSheet = hssfWorkbook.createSheet(xSheet.getSheetName());

                // Merged regions, bounded by legacy limits
                for (int m = 0; m < xSheet.getNumMergedRegions(); m++) {
                    CellRangeAddress region = xSheet.getMergedRegion(m);
                    if (region.getLastRow() < XLS_MAX_ROWS && region.getLastColumn() < XLS_MAX_COLS) {
                        hSheet.addMergedRegion(region);
                    }
                }

                int rowLimit = Math.min(lastRow, XLS_MAX_ROWS - 1);
                for (int r = 0; r <= rowLimit; r++) {
                    XSSFRow xRow = xSheet.getRow(r);
                    if (xRow == null) continue;

                    HSSFRow hRow = hSheet.createRow(r);
                    hRow.setHeightInPoints(xRow.getHeightInPoints());

                    int lastCell = xRow.getLastCellNum();
                    int cellLimit = Math.min(lastCell, XLS_MAX_COLS);
                    for (int c = 0; c < cellLimit; c++) {
                        XSSFCell xCell = xRow.getCell(c);
                        if (xCell == null) continue;

                        HSSFCell hCell = hRow.createCell(c);
                        copyCellValue(xCell, hCell);
                        copyCellStyle(hssfWorkbook, xCell, hCell, styleCache);
                    }
                }

                // Column widths (bounded)
                int maxCols = Math.min(xSheet.getRow(0) != null ? xSheet.getRow(0).getLastCellNum() : 0,
                        XLS_MAX_COLS);
                for (int c = 0; c < maxCols; c++) {
                    hSheet.setColumnWidth(c, xSheet.getColumnWidth(c));
                }
            }

            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            hssfWorkbook.write(bos);
            return bos.toByteArray();
        }
    }

    private void copyCellValue(XSSFCell src, HSSFCell dest) {
        switch (src.getCellType()) {
            case STRING:
                dest.setCellValue(src.getStringCellValue());
                break;
            case NUMERIC:
                dest.setCellValue(src.getNumericCellValue());
                break;
            case BOOLEAN:
                dest.setCellValue(src.getBooleanCellValue());
                break;
            case FORMULA:
                try {
                    dest.setCellFormula(src.getCellFormula());
                } catch (Exception e) {
                    // Fall back to the cached/evaluated value if formula copy fails
                    dest.setCellValue(src.toString());
                }
                break;
            case BLANK:
                dest.setBlank();
                break;
            default:
                dest.setCellValue(src.toString());
        }
    }

    private void copyCellStyle(HSSFWorkbook destWb, XSSFCell src, HSSFCell dest,
                                Map<Integer, HSSFCellStyle> cache) {
        XSSFCellStyle xStyle = src.getCellStyle();
        int key = xStyle.getIndex();

        HSSFCellStyle hStyle = cache.get(key);
        if (hStyle == null) {
            hStyle = destWb.createCellStyle();

            // Font
            XSSFFont xFont = (XSSFFont) src.getSheet().getWorkbook().getFontAt(xStyle.getFontIndex());
            HSSFFont hFont = destWb.createFont();
            hFont.setBold(xFont.getBold());
            hFont.setItalic(xFont.getItalic());
            hFont.setFontHeight(xFont.getFontHeight());
            hFont.setFontName(xFont.getFontName());
            try {
                if (xFont.getXSSFColor() != null) {
                    byte[] rgb = xFont.getXSSFColor().getRGB();
                    if (rgb != null) {
                        hFont.setColor(closestHssfColor(destWb, rgb).getIndex());
                    }
                }
            } catch (Exception ignore) { /* keep default font color */ }
            hStyle.setFont(hFont);

            // Fill color
            try {
                if (xStyle.getFillForegroundColorColor() instanceof XSSFColor) {
                    byte[] rgb = ((XSSFColor) xStyle.getFillForegroundColorColor()).getRGB();
                    if (rgb != null) {
                        hStyle.setFillForegroundColor(closestHssfColor(destWb, rgb).getIndex());
                        hStyle.setFillPattern(FillPatternType.SOLID_FOREGROUND);
                    }
                }
            } catch (Exception ignore) { /* keep default fill */ }

            // Borders
            hStyle.setBorderTop(xStyle.getBorderTop());
            hStyle.setBorderBottom(xStyle.getBorderBottom());
            hStyle.setBorderLeft(xStyle.getBorderLeft());
            hStyle.setBorderRight(xStyle.getBorderRight());

            // Alignment
            hStyle.setAlignment(xStyle.getAlignment());
            hStyle.setVerticalAlignment(xStyle.getVerticalAlignment());
            hStyle.setWrapText(xStyle.getWrapText());

            cache.put(key, hStyle);
        }
        dest.setCellStyle(hStyle);
    }

    private HSSFColor closestHssfColor(HSSFWorkbook wb, byte[] rgb) {
        HSSFPalette palette = wb.getCustomPalette();
        return palette.findSimilarColor(rgb[0] & 0xFF, rgb[1] & 0xFF, rgb[2] & 0xFF);
    }
}
